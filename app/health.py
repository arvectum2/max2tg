"""Local health supervision for the personal MAX2TG bridge.

No credentials, message bodies, recipient identities or raw events are written
into the health report. launchd remains responsible for process restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
MAX_SOCKET_IDLE_SEC = 240
SOCKET_REPAIR_COOLDOWN = 300
TG_REPAIR_COOLDOWN = 120
ALERT_COOLDOWN = 3600
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
    lines = [
        "🩺 <b>MAX2TG — состояние</b>",
        f"MAX: {labels.get(max_status,'⚠️')} {max_status}",
        f"Telegram: {'✅ работает' if tg.get('polling') else ('⏸️ ожидает MAX' if tg.get('paused_expected') else '⚠️ polling недоступен')}",
        f"Очередь: ожидают {counts.get('RECEIVED',0)+counts.get('RETRY',0)}; "
        f"неопределённые {counts.get('UNKNOWN',0)}; ошибки {counts.get('FAILED',0)}",
        f"Изменения: pending {failures.get('PENDING',0)}, "
        f"unknown {failures.get('UNKNOWN',0)}, blocked {failures.get('BLOCKED',0)}",
        f"MAX-темы: {report.get('topics',0)} | курсоры: {report.get('cursors',0)}",
        f"Проблемы догрузки: {report.get('history_gaps',0)}",
        f"Задержка очереди: {outbox.get('oldest_due_seconds',0)} с",
        f"Диск: {'✅ доступен' if report.get('disk',{}).get('ok') else '⚠️ проблема'}",
    ]
    notes = report.get("issues",[])
    if notes:
        lines.append("Требует внимания: " + ", ".join(notes))
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
        self.started = time.monotonic()
        self._repair_socket_at = 0.0
        self._repair_tg_at = 0.0
        self._tg_repair_attempts = 0
        self._issue_hits: dict[str,int] = {}
        self._alerts = self._read_alerts()
        self.report = None

    def _read_alerts(self) -> dict[str,float]:
        try:
            raw=json.loads(self.alert_path.read_text(encoding="utf-8"))
            return {str(k):float(v) for k,v in raw.items()}
        except (OSError,ValueError,TypeError):
            return {}

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
        paused_expected = (
            self.tg_app is not None
            and not bool(getattr(self.client, "_polling_should_run", True))
        )

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
            "telegram":{"polling":polling,"paused_expected":paused_expected},
            "outbox":{"counts":counts,"oldest_due_seconds":oldest},
            "mutations":mutations,
            "topics":len(self.topics._chats),
            "cursors":len(self.topics._sync),
            "history_gaps":history_gaps,
            "disk":{"ok":disk_ok,"free_bytes":free},
            "issues":issues,
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
        if (tg_down and not report["telegram"].get("paused_expected")
            and self.tg_app and self.tg_app.running and
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
        if time.monotonic()-self.started<STARTUP_GRACE_SEC:
            return
        now=time.time()
        for issue in report["issues"]:
            self._issue_hits[issue]=self._issue_hits.get(issue,0)+1
        for issue in set(self._issue_hits)-set(report["issues"]):
            self._issue_hits.pop(issue,None)
        important={"REAUTH_REQUIRED","OUTBOX_UNKNOWN","OUTBOX_FAILED",
                   "STATE_DISK","TELEGRAM_POLLING","MUTATION_UNKNOWN",
                   "MAX_OFFLINE","MAX_STALLED","QUEUE_DELAY","HISTORY_GAP"}
        alertable=[key for key in report["issues"] if key in important
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

    async def monitor(self, interval=HEALTH_CHECK_INTERVAL):
        while True:
            try:
                report=self.snapshot()
                self.report=report
                self._write_atomic(self.path,report)
                await self.repair(report)
                await self.notify(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Health supervision cycle failed")
            await asyncio.sleep(interval)
