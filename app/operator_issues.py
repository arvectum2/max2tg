"""Read-only and explicitly acknowledged operator triage without payload leaks."""
from __future__ import annotations

from html import escape
import re
import time

PREFIX=re.compile(r"^[0-9a-f]{12,64}$")


def get_issues(outbox,tg_outbound=None,limit=12):
    results=[]
    for row in outbox.db.execute(
        "SELECT key,kind,chat_id,max_id,state,attempts FROM message_mutations "
        "WHERE state IN ('BLOCKED','UNKNOWN') ORDER BY updated DESC LIMIT ?",
        (limit,)):
        part=outbox.db.execute(
            "SELECT COUNT(*) FROM jobs j JOIN parts p ON j.key=p.job_key "
            "WHERE j.chat_id=? AND j.max_id=? AND p.state='SENT'",
            (row["chat_id"],row["max_id"])).fetchone()[0]
        if row["state"]=="BLOCKED" and row["kind"]=="edit" and not part:
            cause="Нет подтверждённого сообщения, созданного ботом"
        elif row["kind"]=="edit" and part>1:
            cause="Редактирование многосоставного сообщения"
        else:
            cause="Нужна ручная сверка"
        results.append({
            "ref":row["key"][:12],"key":row["key"],
            "category":"MAX mutation","kind":row["kind"],
            "state":row["state"],"cause":cause,"attempts":row["attempts"],
        })
    for row in outbox.db.execute(
        "SELECT key,status FROM jobs WHERE status IN ('UNKNOWN','FAILED') "
        "ORDER BY updated DESC LIMIT ?",(limit,)):
        results.append({"ref":row["key"][:12],"key":row["key"],
                        "category":"MAX→Telegram","kind":"delivery",
                        "state":row["status"],"cause":"Неясная отправка",
                        "attempts":None})
    if tg_outbound:
        for row in tg_outbound.db.execute(
            "SELECT status,telegram_id FROM outbound "
            "WHERE status IN ('UNKNOWN','FAILED') "
            "ORDER BY updated DESC LIMIT ?",(limit,)):
            results.append({"ref":None,"key":None,
                            "category":"Telegram→MAX","kind":"delivery",
                            "state":row["status"],"cause":"Неясная отправка",
                            "attempts":None})
    return results[:limit]


def format_issues(issues):
    lines=["🛠 <b>MAX2TG — диагностика операций</b>"]
    if not issues:
        return lines[0]+"\nНеопределённых и заблокированных операций нет."
    for i,entry in enumerate(issues,1):
        name=entry["category"]
        lines.append(
            f"{i}. <b>{escape(name)} · {escape(entry['state'])}</b>"
            f" ({escape(entry['kind'])})\n"
            f"Причина: {escape(entry['cause'])}"
            + (f"\nКод: <code>{entry['ref']}</code>" if entry["ref"] else "")
        )
    lines.append(
        "\nКоманда <code>/issue_ack КОД</code> только для BLOCKED правки "
        "помечает её как просмотренную, <b>но не применённую</b>. "
        "UNKNOWN автоматически не повторяется. "
        "Коды являются только локальными идентификаторами диагностики."
    )
    return "\n\n".join(lines)


def acknowledge_blocked_mutation(outbox,reference:str):
    """Mark explicit review without claiming Telegram was edited."""
    if not isinstance(reference,str) or not PREFIX.fullmatch(reference):
        raise ValueError("Неверный код операции")
    matches=outbox.db.execute(
        "SELECT key FROM message_mutations "
        "WHERE key LIKE ? AND state='BLOCKED'",
        (reference+"%",)).fetchall()
    if len(matches)!=1:
        raise ValueError("Нет одной заблокированной операции с этим кодом")
    outbox.db.execute(
        "UPDATE message_mutations SET state='REVIEWED',updated=? "
        "WHERE key=? AND state='BLOCKED'",
        (time.time(),matches[0]["key"]))
    return True
