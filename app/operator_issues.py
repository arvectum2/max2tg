"""Read-only paginated operator triage without private payloads or IDs."""
from __future__ import annotations
from html import escape
import hashlib
import json
import re
import time

PREFIX=re.compile(r'^[0-9a-f]{12,64}$')
PAGE_SIZE=8


def _ref(category,key):
    return hashlib.sha256(json.dumps([category,key],ensure_ascii=False).encode()).hexdigest()[:16]


def _receipt_count(parts):
    ids=set()
    for p in parts:
        if p['state']!='SENT':continue
        try:values=json.loads(p['tg_ids']) if p['tg_ids'] else [p['tg_id']]
        except (ValueError,TypeError):continue
        if isinstance(values,list):ids.update(i for i in values if type(i) is int and i>0)
    return len(ids)


def get_issues(outbox,tg_outbound=None,limit=12,tg_mutations=None,*,offset=0):
    """Combine all categories before slicing: no permanent category starvation."""
    limit=max(1,min(100,int(limit)));offset=max(0,int(offset));results=[]
    for row in outbox.db.execute("SELECT key,kind,chat_id,max_id,state,attempts,updated FROM message_mutations WHERE state IN ('BLOCKED','UNKNOWN')"):
        part=outbox.db.execute("SELECT COUNT(*) FROM jobs j JOIN parts p ON j.key=p.job_key WHERE j.chat_id=? AND j.max_id=? AND p.state='SENT'",(row['chat_id'],row['max_id'])).fetchone()[0]
        cause='Нужна ручная сверка'
        if row['state']=='BLOCKED' and row['kind']=='edit' and not part:cause='Нет подтверждённого сообщения, созданного ботом'
        elif row['kind']=='edit' and part>1:cause='Редактирование многосоставного сообщения'
        results.append(dict(ref=row['key'][:12],key=row['key'],category='MAX mutation',kind=row['kind'],state=row['state'],cause=cause,attempts=row['attempts'],updated=row['updated'],ackable=row['state']=='BLOCKED'))
    for row in outbox.db.execute("SELECT key,status,last_error,attempts,updated FROM jobs WHERE status IN ('UNKNOWN','FAILED')"):
        parts=outbox.db.execute('SELECT state,tg_id,tg_ids FROM parts WHERE job_key=?',(row['key'],)).fetchall();n=_receipt_count(parts)
        cause='Доставка не подтверждена; не повторять' if row['status']=='UNKNOWN' else 'Отправка завершилась ошибкой'
        if row['last_error']=='outbound part changed after restart':cause='Изменился состав отправки после перезапуска'
        elif row['last_error']=='Telegram rejected request':cause='Telegram отклонил запрос'
        elif row['last_error']=='MAX video original unavailable after bounded attempts':cause='Исходное видео недоступно'
        if n:cause+=f'; подтверждённых частей: {n}'
        results.append(dict(ref=_ref('MAX delivery',row['key']),category='MAX→Telegram',kind='delivery',state=row['status'],cause=cause,attempts=row['attempts'],updated=row['updated'],ackable=False))
    if tg_outbound:
        for row in tg_outbound.db.execute("SELECT status,telegram_chat,telegram_id,updated FROM outbound WHERE status IN ('UNKNOWN','FAILED') OR (status='BLOCKED' AND album_pending=0 AND album_id IS NOT NULL)"):
            cause={'UNKNOWN':'Результат отправки неизвестен; не повторять','FAILED':'Ошибка подготовки или отказ MAX','BLOCKED':'Альбом требует ручной сверки'}[row['status']]
            results.append(dict(ref=_ref('TG delivery',[row['telegram_chat'],row['telegram_id']]),category='Telegram→MAX',kind='delivery',state=row['status'],cause=cause,attempts=None,updated=row['updated'],ackable=False))
    if tg_mutations:
        for row in tg_mutations.db.execute("SELECT key,kind,status,updated FROM tg_mutations WHERE status IN ('UNKNOWN','FAILED')"):
            results.append(dict(ref=_ref('TG mutation',row['key']),category='Telegram→MAX изменение',kind=row['kind'],state=row['status'],cause='Нужна ручная сверка, повтор запрещён',attempts=None,updated=row['updated'],ackable=False))
    results.sort(key=lambda r:(-r['updated'],r['category'],r['ref']))
    return results[offset:offset+limit]


def format_issues(issues,*,page=1,has_next=False):
    lines=['🛠 <b>MAX2TG — диагностика операций</b>']
    if not issues:
        return lines[0]+('\nНа этой странице записей нет. /issues 1' if page>1 else '\nНеопределённых и заблокированных операций нет.')
    for i,entry in enumerate(issues,1+(page-1)*PAGE_SIZE):
        lines.append(f"{i}. <b>{escape(entry['category'])} · {escape(entry['state'])}</b> ({escape(entry['kind'])})\nПричина: {escape(entry['cause'])}"+ (f"\nКод: <code>{entry['ref']}</code>" if entry['ref'] else '')+ (' · доступен /issue_ack' if entry.get('ackable') else ''))
    nav=f'Страница {page}.'
    if page>1:nav+=f' Назад: /issues {page-1}.'
    if has_next:nav+=f' Далее: /issues {page+1}.'
    lines.append(nav+'\n/issue_ack КОД — только просмотр BLOCKED правки, не её применение. UNKNOWN автоматически не повторяется.')
    return '\n\n'.join(lines)


def acknowledge_blocked_mutation(outbox,reference:str):
    """Mark explicit review without claiming Telegram was edited."""
    if not isinstance(reference,str) or not PREFIX.fullmatch(reference):raise ValueError('Неверный код операции')
    matches=outbox.db.execute("SELECT key FROM message_mutations WHERE key LIKE ? AND state='BLOCKED'",(reference+'%',)).fetchall()
    if len(matches)!=1:raise ValueError('Нет одной заблокированной операции с этим кодом')
    outbox.db.execute("UPDATE message_mutations SET state='REVIEWED',updated=? WHERE key=? AND state='BLOCKED'",(time.time(),matches[0]['key']))
    return True
