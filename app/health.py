"""Local health supervision for the personal MAX2TG bridge.

No credentials, message bodies, recipient identities or raw events are written
into the health report. launchd remains responsible for process restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from app.tg_ingress import DurableIngressQueue
from app.chat_preferences import get_chat_prefs

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
MAX_SOCKET_IDLE_SEC = 240
SOCKET_REPAIR_COOLDOWN = 300
TG_REPAIR_COOLDOWN = 120
ALERT_COOLDOWN = 3600
# Acknowledged historical incidents are visible in /status, but must not
# generate hourly notifications. A different set of affected ledger records
# is treated as a NEW incident and is alerted once.
ACKNOWLEDGEABLE_ISSUES = frozenset({
    "TG_OUTBOUND_FAILED", "OUTBOX_UNKNOWN", "MUTATION_BLOCKED", "HISTORY_GAP",
})
STARTUP_GRACE_SEC = 120
OUTBOX_OLD_AGE_SEC = 300
MIN_DISK_FREE_BYTES = 100 * 1024 * 1024


def status_text(report: dict | None) -> str:
    """Human-readable diagnostics without secrets or private message data."""
    if not report:
        return "🩺 MAX2TG: проверка состояния ещё не выполнена."
    labels = {
        "ONLINE":"✅", "RECONNECTING":"🔄", "REAUTH_REQUIRED":"🔐",
        "STALLED":"⚠️", "CONNECTING":"⏳",
    }
    max_status = report.get("max",{}).get("state","CONNECTING")
    tg = report.get("telegram",{})
    outbox = report.get("outbox",{})
    counts = outbox.get("counts",{})
    failures = report.get("mutations",{})
    tg_out=report.get("tg_outbound",{})
    tg_mut=report.get("tg_mutations",{})
    lines = [
        "🩺 <b>MAX2TG — состояние</b>",
        f"MAX: {labels.get(max_status,'⚠️')} {max_status}",
        f"Telegram: {'✅ работает' if tg.get('polling') else ('⏸️ ожидает MAX' if tg.get('paused_expected') else '⚠️ polling недоступен')}",
        f"Очередь: ожидают {counts.get('RECEIVED',0)+counts.get('RETRY',0)}; "
        f"неопределённые {counts.get('UNKNOWN',0)}; ошибки {counts.get('FAILED',0)}",
        f"Telegram → MAX: ожидают {tg_out.get('RECEIVED',0)}; "
        f"неопределённые {tg_out.get('UNKNOWN',0)}; "
        f"ошибки {tg_out.get('FAILED',0)}",
        f"Telegram: необработанный входящий буфер {report.get('tg_ingress_pending',0)}",
        f"TG→MAX правки/реакции: unknown {tg_mut.get('UNKNOWN',0)}, "
        f"failed {tg_mut.get('FAILED',0)}",
        f"Изменения: pending {failures.get('PENDING',0)}, "
        f"unknown {failures.get('UNKNOWN',0)}, blocked {failures.get('BLOCKED',0)}",
        f"MAX-темы: {report.get('topics',0)} | курсоры: {report.get('cursors',0)}",
        f"Проблемы догрузки: {report.get('history_gaps',0)}",
        f"Задержка очереди: {outbox.get('oldest_due_seconds',0)} с",
        f"Диск: {'✅ доступен' if report.get('disk',{}).get('ok') else '⚠️ проблема'}",
    ]
    notes = report.get("issues",[])
    acknowledged = set(report.get("acknowledged_issues",[]))
    active = [item for item in notes if item not in acknowledged]
    historical = [item for item in notes if item in acknowledged]
    if active:
        lines.append("Требует внимания: " + ", ".join(active))
    if historical:
        lines.append("Исторические (уведомления отключены): " + ", ".join(historical))
    return "\n".join(lines)


class HealthSupervisor:
    def __init__(self, client, sender, outbox, topics, tg_app, state_dir: str):
        self.client = client
        self.sender = sender
        self.outbox = outbox
        self.topics = topics
        self.tg_app = tg_app
        self.dir = Path(state_dir)
        self.path = self.dir / "health.json"
        self.alert_path = self.dir / "health-alerts.json"
        self.silence_path = self.dir / "health-silences.json"
        self.started = time.monotonic()
        self._repair_socket_at = 0.0
        self._repair_tg_at = 0.0
        self._tg_repair_attempts = 0
        self._issue_hits: dict[str,int] = {}
        self._alerts = self._read_alerts()
        self._silenced_issues = self._read_silences()
        self.report = None

    def _read_alerts(self) -> dict[str,float]:
        try:
            raw=json.loads(self.alert_path.read_text(encoding="utf-8"))
            return {str(k):float(v) for k,v in raw.items()}
        except (OSError,ValueError,TypeError):
            return {}

    def _read_silences(self) -> dict[str,str]:
        try:
            raw = json.loads(self.silence_path.read_text(encoding="utf-8"))
            if not isinstance(raw,dict):
                return {}
            return {k:v for k,v in raw.items()
                    if k in ACKNOWLEDGEABLE_ISSUES and isinstance(v,str)
                    and len(v)==64 and all(c in "0123456789abcdef" for c in v)}
        except (OSError,ValueError,TypeError):
            return {}

    def _issue_signature(self, issue: str) -> str | None:
        """Fingerprint affected operation IDs, not payloads or timestamps.

        A reconnect can change warning timestamps without creating a new
        incident. Never include those timestamps in notification identity.
        """
        if issue == "OUTBOX_UNKNOWN":
            values = [row[0] for row in self.outbox.db.execute(
                "SELECT key FROM jobs WHERE status='UNKNOWN' ORDER BY key")]
        elif issue == "MUTATION_BLOCKED":
            values = [row[0] for row in self.outbox.db.execute(
                "SELECT key FROM message_mutations WHERE state='BLOCKED' ORDER BY key")]
        elif issue == "TG_OUTBOUND_FAILED":
            tg = getattr(self,"tg_outbound",None)
            if tg is None:
                return None
            values = [list(row) for row in tg.db.execute(
                "SELECT telegram_chat,telegram_id FROM outbound "
                "WHERE status='FAILED' ORDER BY telegram_chat,telegram_id")]
        elif issue == "HISTORY_GAP":
            warnings = self.topics.get_ui("catch_up_warnings",{}) or {}
            if not isinstance(warnings,dict):
                return None
            values = sorted((str(k), str(v.get("reason","")) if isinstance(v,dict)
                             else str(v)) for k,v in warnings.items())
        else:
            return None
        if not values:
            return None
        raw = json.dumps(values,ensure_ascii=False,separators=(",",":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def acknowledge_existing(self, issues) -> list[str]:
        """Silence existing incidents; never alter message ledgers/cursors."""
        acknowledged = []
        for issue in issues:
            if issue not in ACKNOWLEDGEABLE_ISSUES:
                continue
            signature = self._issue_signature(issue)
            if signature:
                self._silenced_issues[issue] = signature
                acknowledged.append(issue)
        if acknowledged:
            self._write_atomic(self.silence_path,self._silenced_issues)
        return acknowledged

    def _acknowledged_issues(self, issues) -> list[str]:
        return [issue for issue in issues if issue in ACKNOWLEDGEABLE_ISSUES
                and self._silenced_issues.get(issue)==self._issue_signature(issue)]

    def _write_atomic(self, path: Path, payload: dict):
        self.dir.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix=".max2tg-health-",dir=self.dir)
        os.fchmod(fd,0o600)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as out:
                json.dump(payload,out,ensure_ascii=False,indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def snapshot(self) -> dict:
        now=time.monotonic()
        socket=getattr(self.client,"_ws",None)
        ws_open = bool(socket is not None and not socket.closed)
        authorized=bool(getattr(self.client,"_authorized",False))
        auth_required=bool(getattr(self.client,"_auth_required",False))
        rx=getattr(self.client,"_last_rx_monotonic",None)
        age=round(max(0,now-rx)) if rx is not None and ws_open else None
        if auth_required:
            max_state="REAUTH_REQUIRED"
        elif not ws_open:
            max_state="RECONNECTING"
        elif not authorized:
            max_state="CONNECTING"
        elif age is not None and age > MAX_SOCKET_IDLE_SEC:
            max_state="STALLED"
        else:
            max_state="ONLINE"

        polling=bool(self.tg_app and self.tg_app.running and
                     self.tg_app.updater and self.tg_app.updater.running)
        if self.tg_app is None:
            polling=True # REPLY_ENABLED=false intentionally disables polling.
        request = getattr(self.tg_app, "bot_data", {}).get("polling_request") if self.tg_app else None
        poll_age = round(request.age()) if request is not None else None
        poll_stale = bool(request is not None and request.stale())
        poll_verified = request is None or request.last_success is not None
        paused_expected = (
            self.tg_app is not None
            and not bool(getattr(self.client, "_polling_should_run", True))
        )
        polling = polling and not poll_stale and poll_verified

        counts=self.outbox.counts()
        row=self.outbox.db.execute(
            "SELECT MIN(created) FROM jobs WHERE status IN ('RECEIVED','RETRY') "
            "AND next_attempt<=?",(time.time(),)
        ).fetchone()
        oldest=int(max(0,time.time()-row[0])) if row and row[0] else 0
        try:
            mutations={str(k):int(v) for k,v in self.outbox.db.execute(
                "SELECT state,COUNT(*) FROM message_mutations GROUP BY state"
            ).fetchall()}
        except Exception:
            mutations={}
        try:
            free=shutil.disk_usage(self.dir).free
            disk_ok=os.access(self.dir,os.W_OK) and free>=MIN_DISK_FREE_BYTES
        except OSError:
            free=0
            disk_ok=False
        issues=[]
        if auth_required: issues.append("REAUTH_REQUIRED")
        if max_state=="STALLED": issues.append("MAX_STALLED")
        if max_state=="RECONNECTING" and now-self.started>180:
            issues.append("MAX_OFFLINE")
        if not polling and not paused_expected: issues.append("TELEGRAM_POLLING")
        if not disk_ok: issues.append("STATE_DISK")
        if oldest>OUTBOX_OLD_AGE_SEC: issues.append("QUEUE_DELAY")
        tg_mutations=(getattr(self,"tg_mutations",None).counts()
                      if getattr(self,"tg_mutations",None) else {})
        tg_counts=(getattr(self,"tg_outbound",None).counts()
                   if getattr(self,"tg_outbound",None) else {})
        if tg_mutations.get("UNKNOWN",0): issues.append("TG_MUTATION_UNKNOWN")
        if tg_mutations.get("FAILED",0): issues.append("TG_MUTATION_FAILED")
        if tg_counts.get("UNKNOWN",0): issues.append("TG_OUTBOUND_UNKNOWN")
        if tg_counts.get("FAILED",0): issues.append("TG_OUTBOUND_FAILED")
        ingress = getattr(self.tg_app,"update_queue",None) if self.tg_app else None
        pending_ingress = ingress.pending_count() if isinstance(
            ingress, DurableIngressQueue) else 0
        if isinstance(ingress, DurableIngressQueue) and ingress.pending_count(older_than=120):
            issues.append("TG_INGRESS_STALLED")
        tg_ledger = getattr(self,"tg_outbound",None)
        if tg_ledger is not None and max_state == "ONLINE":
            overdue = tg_ledger.db.execute(
                "SELECT max_chat,payload FROM outbound WHERE status='RECEIVED' AND created<=?",
                (time.time()-120,),
            ).fetchall()
            if any(self.topics.get_topic(r[0]) is not None
                   and get_chat_prefs(self.topics,r[0]).sync
                   and ("media" not in json.loads(r[1]) or get_chat_prefs(self.topics,r[0]).media)
                   for r in overdue):
                issues.append("TG_OUTBOUND_QUEUE_DELAY")
        if counts.get("UNKNOWN",0): issues.append("OUTBOX_UNKNOWN")
        if counts.get("FAILED",0): issues.append("OUTBOX_FAILED")
        if mutations.get("UNKNOWN",0): issues.append("MUTATION_UNKNOWN")
        if mutations.get("BLOCKED",0): issues.append("MUTATION_BLOCKED")
        pending_history = self.topics.get_ui("catch_up_warnings", {}) or {}
        history_gaps = len(pending_history) if isinstance(pending_history,dict) else 0
        if history_gaps: issues.append("HISTORY_GAP")

        return {
            "time":datetime.now(timezone.utc).isoformat(),
            "uptime_seconds":int(now-self.started),
            "max":{"state":max_state,"ws_connected":ws_open,
                   "authorized":authorized,"rx_age_seconds":age},
            "telegram":{"polling":polling,"paused_expected":paused_expected,
                        "poll_age_seconds":poll_age,"poll_stale":poll_stale},
            "outbox":{"counts":counts,"oldest_due_seconds":oldest},
            "tg_outbound":tg_counts,
            "tg_ingress_pending":pending_ingress,
            "tg_mutations":tg_mutations,
            "mutations":mutations,
            "topics":len(self.topics._chats),
            "cursors":len(self.topics._sync),
            "history_gaps":history_gaps,
            "disk":{"ok":disk_ok,"free_bytes":free},
            "issues":issues,
            "acknowledged_issues":self._acknowledged_issues(issues),
        }

    async def repair(self, report: dict):
        now=time.monotonic()
        state=report["max"]["state"]
        socket=getattr(self.client,"_ws",None)
        if (state=="STALLED" and socket is not None
            and now-self._repair_socket_at>=SOCKET_REPAIR_COOLDOWN):
            self._repair_socket_at=now
            log.warning("Health: stale MAX socket; requesting reconnect")
            try:
                await socket.close()
            except Exception:
                log.exception("Health: cannot close stale MAX socket")
        tg_down=not report["telegram"]["polling"]
        if report["telegram"].get("poll_stale") and self.tg_app:
            # A running-but-dead Updater may hold a stranded HTTP pool.
            # Recreate the whole worker/transport via the launchd supervisor.
            restart = getattr(self, "restart_worker", None)
            if restart is not None and now-self._repair_tg_at>=TG_REPAIR_COOLDOWN:
                self._repair_tg_at=now
                log.error("Telegram getUpdates has no successful response; restarting worker")
                restart()
                return
        if (tg_down and not report["telegram"].get("paused_expected")
            and self.tg_app and self.tg_app.running and
            not self.tg_app.updater.running and
            now-self._repair_tg_at>=TG_REPAIR_COOLDOWN
            and self._tg_repair_attempts<3):
            self._repair_tg_at=now
            self._tg_repair_attempts+=1
            try:
                await self.tg_app.updater.start_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
                log.info("Health: restored Telegram polling")
                self._tg_repair_attempts=0
            except Exception:
                log.exception("Health: Telegram polling restart failed")
        elif not tg_down:
            self._tg_repair_attempts=0

    async def notify(self, report: dict):
        stale_poll = report.get("telegram",{}).get("poll_stale",False)
        in_grace = time.monotonic()-self.started<STARTUP_GRACE_SEC
        if in_grace and not stale_poll:
            return
        now=time.time()
        for issue in report["issues"]:
            self._issue_hits[issue]=self._issue_hits.get(issue,0)+1
        for issue in set(self._issue_hits)-set(report["issues"]):
            self._issue_hits.pop(issue,None)
        important={"REAUTH_REQUIRED","OUTBOX_UNKNOWN","OUTBOX_FAILED",
                   "STATE_DISK","TELEGRAM_POLLING","MUTATION_UNKNOWN",
                   "MAX_OFFLINE","MAX_STALLED","QUEUE_DELAY","HISTORY_GAP",
                   "TG_OUTBOUND_UNKNOWN","TG_OUTBOUND_FAILED",
                   "TG_MUTATION_UNKNOWN","TG_MUTATION_FAILED",
                   "TG_INGRESS_STALLED","TG_OUTBOUND_QUEUE_DELAY"}
        if stale_poll and "TELEGRAM_POLLING" in report["issues"]:
            self._issue_hits["TELEGRAM_POLLING"] = max(3,self._issue_hits.get("TELEGRAM_POLLING",0))
        if in_grace:
            important = {"TELEGRAM_POLLING"}
        acknowledged = set(self._acknowledged_issues(report["issues"]))
        alertable=[key for key in report["issues"] if key in important
                   and key not in acknowledged
                   and self._issue_hits.get(key,0)>=3
                   and now-self._alerts.get(key,0)>=ALERT_COOLDOWN]
        if not alertable:
            return
        markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🩺 Проверить",callback_data="health:refresh"),
            InlineKeyboardButton("🔐 Вход в MAX",callback_data="health:reauth"),
        ]])
        try:
            await self.sender.bot.send_message(
                chat_id=int(self.sender.chat_id),
                text="⚠️ <b>MAX2TG требует внимания</b>\n"
                     + "\n".join(f"• {key}" for key in alertable)
                     + "\n/status — подробности.",
                parse_mode="HTML",
                disable_notification=True,
                reply_markup=markup,
            )
        except Exception:
            log.exception("Health: failed sending operator alert")
            return
        for key in alertable:
            self._alerts[key]=now
        self._write_atomic(self.alert_path,self._alerts)
        # New historical ledger incidents are reported once, not every hour.
        self.acknowledge_existing(alertable)

    async def monitor(self, interval=HEALTH_CHECK_INTERVAL):
        while True:
            try:
                report=self.snapshot()
                self.report=report
                self._write_atomic(self.path,report)
                if report["telegram"].get("poll_stale"):
                    # If only getUpdates is broken, the send endpoint may
                    # still reach the owner. Bound the warning before restart.
                    try:
                        await asyncio.wait_for(self.notify(report),timeout=8)
                    finally:
                        await self.repair(report)
                else:
                    await self.repair(report)
                    await self.notify(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Health supervision cycle failed")
            await asyncio.sleep(interval)
