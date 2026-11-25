"""Explicit, bounded photo backfill through the existing durable ledger."""
import asyncio
import json
from app.chat_preferences import get_chat_prefs
from app.history_nav import history_page
from app.history_ui import _from_context, _is_admin, _topic_scope
from app.max_listener import import_recent_history
from app.max_media import photo_url
from app.outbox import OutboxStore


def photo_count(row):
    attaches = row.get('attaches') or []
    link = row.get('link') or {}
    if isinstance(link, dict) and link.get('type') == 'FORWARD':
        inner = link.get('message') or link
        if not isinstance(inner, dict):
            return 0
        # Match the existing forwarding path exactly: it sends nested
        # attachments, not the outer message's duplicate metadata.
        attaches = inner.get('attaches') or link.get('attaches') or []
    meaningful = [a for a in attaches if isinstance(a, dict) and a.get('_type') not in ('CONTROL','WIDGET','INLINE_KEYBOARD','SHARE',None)]
    # Reject mixed video/document posts rather than counting preview photos
    # as originals. Such posts require a separate per-attachment ledger.
    if not meaningful or any(a.get('_type') != 'PHOTO' or not photo_url(a) for a in meaningful):
        return 0
    return len(meaningful)


def confirmed_photos(db, key):
    total = 0
    for part in db.db.execute('SELECT method,state,tg_id,tg_ids FROM parts WHERE job_key=?',(key,)):
        if part['state'] != 'SENT' or not part['tg_id']:
            continue
        if part['method'] == 'send_photo':
            total += 1
        elif part['method'] == 'send_media_group' and part['tg_ids']:
            total += len(set(json.loads(part['tg_ids'])))
    return total


async def import_photos(client, sender, resolver, chat_id, thread_id):
    store = sender.topic_store
    db = sender.outbox
    if not isinstance(db, OutboxStore) or store.get_topic(chat_id) != thread_id:
        raise RuntimeError('Missing durable ledger or chat binding')
    if not get_chat_prefs(store,chat_id).sync or not get_chat_prefs(store,chat_id).media:
        raise RuntimeError('Photo sync is disabled')
    stats = dict(scanned=0, candidates=0, delivered=0, existing=0, unresolved=0)
    async with client._history_locks.setdefault(str(chat_id),asyncio.Lock()):
        marker = None
        rows = {}
        for _ in range(3):
            page, nxt, end = await history_page(client,chat_id,marker,count=100)
            stats['scanned'] += len(page)
            for row in page:
                rows.setdefault(str(row['id']),row)
            if end:
                break
            if nxt is None or nxt == marker:
                raise RuntimeError('History cursor did not advance')
            marker = nxt
        for mid,row in sorted(rows.items(),key=lambda kv: (kv[1]['time'],kv[0])):
            count = photo_count(row)
            if not count:
                continue
            stats['candidates'] += count
            key = db.job_key(chat_id,mid)
            previous = db.job(key)
            if previous is not None or store.tg_for_max_message(chat_id,mid) is not None:
                if previous is not None and previous['status']=='SENT' and confirmed_photos(db,key)==count:
                    stats['existing'] += count
                else:
                    stats['unresolved'] += count
                continue
            # Leave the original MAX ID and payload intact; never reset or
            # retry a pre-existing uncertain delivery, even if no mapping exists.
            await import_recent_history(client,sender,resolver,chat_id,thread_id,1,raw_messages=[row])
            current = db.job(key)
            actual = confirmed_photos(db,key)
            if current is not None and current['status']=='SENT' and actual==count:
                stats['delivered'] += count
            else:
                stats['unresolved'] += count
                break
    return stats


async def cmd_photos(update,context):
    msg = update.message
    if msg is None or not _is_admin(update,context):
        return
    client,store,_ = _from_context(context)
    cid = _topic_scope(msg,store) if store else None
    if cid is None or client is None or not getattr(client,'_authorized',False):
        await msg.reply_text('Открой тему MAX-чата; мост должен быть подключён.')
        return
    sender = getattr(client,'_tg_sender',None)
    resolver = getattr(client,'resolver',None)
    if sender is None or resolver is None:
        return
    try:
        s = await import_photos(client,sender,resolver,cid,msg.message_thread_id)
    except RuntimeError:
        await msg.reply_text('Импорт фото остановлен: история или настройки недоступны. Не повторяй неопределённые отправки.')
        return
    await msg.reply_text(f"Фото: просмотрено {s['scanned']} сообщений; найдено {s['candidates']}; доставлено {s['delivered']}; уже доставлено {s['existing']}; не подтверждено {s['unresolved']}.")
