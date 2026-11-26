"""Stopped, trusted-operator receipt reconciliation; no network or credentials.
Never restores authorization or changes central evidence. Requires operator
quiescence and existing cooperative runtime/provisioning locks.
"""
import argparse,hashlib,hmac,json,sqlite3,time
from contextlib import ExitStack,closing
from app.recovery_manifest import _private_dir,_file
from app.coordinated_backup import _disjoint,_lock
from app.capsule_registry import TENANT_ID
from app.telegram_egress import SOURCE_KEY
from app.telegram_control_plane import TelegramControlPlane


def _connect(root,name,mode):
    path=_file(root,name)
    for ext in ('-wal','-shm','-journal'):
        side=root/(name+ext)
        if side.exists() or side.is_symlink():_file(root,name+ext)
    db=sqlite3.connect(path.as_uri()+'?mode='+mode,uri=True,isolation_level=None,timeout=1)
    db.row_factory=sqlite3.Row
    return db


def _hash(text):return hashlib.sha256(text.encode()).hexdigest()


def reconcile(registry,state,sid,*,provisioning_stopped=False,apply=False):
    if provisioning_stopped is not True:raise PermissionError('Provisioning must be stopped')
    if type(sid) is not str or not TENANT_ID.fullmatch(sid) or type(apply) is not bool:raise ValueError('Explicit capsule and mode required')
    root=_private_dir(registry);worker=_private_dir(state);_disjoint([root,worker])
    with ExitStack() as stack:
        for path,name in ((root,'shared-gateway.lock'),(root,'shared-provisioning.lock'),(worker,'worker.lock')):_lock(stack,path,name)
        reg=stack.enter_context(closing(_connect(root,'capsules.sqlite3','ro')))
        central=stack.enter_context(closing(_connect(root,'telegram-egress.sqlite3','ro')))
        local=stack.enter_context(closing(_connect(worker,'to-telegram.sqlite3','rw' if apply else 'ro')))
        for db in (reg,central,local):
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite evidence')
        row=reg.execute('''SELECT c.desired_active,c.telegram_mode,c.credential_mode,c.tg_chat_id,o.stage,o.actor,o.forum,r.status,r.tg_chat_id AS route_forum,t.tg_user_id
            FROM capsules c JOIN shared_onboarding o ON o.sid=c.session_id JOIN tg_routes r ON r.session_id=c.session_id AND r.user_id=c.user_id JOIN tg_owners t ON t.user_id=c.user_id WHERE c.session_id=?''',(sid,)).fetchone()
        if not row or tuple(row)[:3]!=(0,'shared','external') or row['stage']!='REVOKED' or row['status']!='DISABLED' or not row['tg_chat_id']==row['forum']==row['route_forum'] or row['actor']!=row['tg_user_id']:raise PermissionError('Exact revoked external capsule required')
        acl=json.loads(_file(worker,'worker-acl.json').read_text())
        if acl!={'sid':sid,'actor':int(row['actor']),'forum':int(row['forum'])}:raise PermissionError('Worker ACL mismatch')
        # No constructor: never create/migrate a ledger or recover SENDING here.
        if apply:local.execute('PRAGMA synchronous=FULL');local.execute('BEGIN IMMEDIATE')
        try:
            pending=local.execute("SELECT * FROM worker_telegram_intents WHERE status='ACCEPTED' ORDER BY source_key LIMIT 1001").fetchall()
            if len(pending)>1000:raise ValueError('Review limit exceeded')
            result={'SENT':0,'BLOCKED':0,'UNKNOWN':0,'unresolved':0,'applied':apply}
            changes=[]
            for w in pending:
                key=w['source_key'];cid=w['max_chat_id'];mid=w['max_message_id'];text=w['text']
                if not isinstance(key,str) or not SOURCE_KEY.fullmatch(key) or not isinstance(mid,str) or not SOURCE_KEY.fullmatch(mid) or not isinstance(text,str) or not text or w['submission_attempted']!=1 or w['telegram_id'] is not None:raise ValueError('Invalid accepted intent evidence')
                if not isinstance(cid,str) or TelegramControlPlane.max_chat_key(cid)!=cid or key!='max:'+_hash(json.dumps([cid,mid],separators=(',',':'))) or not hmac.compare_digest(w['fingerprint'],_hash(cid+'\0'+mid+'\0'+text)):raise ValueError('Worker identity conflict')
                c=central.execute('SELECT * FROM telegram_egress WHERE session_id=? AND source_key=?',(sid,key)).fetchone()
                if c is None:result['unresolved']+=1;continue
                topic=reg.execute('SELECT tg_chat_id,thread_id FROM tg_topic_routes WHERE session_id=? AND max_chat_id=?',(sid,cid)).fetchone()
                if not topic or tuple(topic)!=(row['forum'],c['thread_id']) or c['tg_chat_id']!=row['forum'] or c['max_chat_id']!=cid or c['text']!=text or type(c['thread_id']) is not int or c['thread_id']<=1 or not hmac.compare_digest(c['fingerprint'],_hash(row['forum']+'\0'+cid+'\0'+str(c['thread_id'])+'\0'+text)):raise ValueError('Central receipt identity conflict')
                status=c['status'];tid=c['telegram_id']
                if status not in ('SENT','BLOCKED','UNKNOWN'):result['unresolved']+=1;continue
                if (status=='SENT' and (type(tid) is not int or tid<=0)) or (status!='SENT' and tid is not None):raise ValueError('Invalid terminal receipt')
                changes.append((status,tid,key));result[status]+=1
            if apply:
                for status,tid,key in changes:
                    changed=local.execute("UPDATE worker_telegram_intents SET status=?,telegram_id=?,updated_at=? WHERE source_key=? AND status='ACCEPTED' AND submission_attempted=1",(status,tid,time.time(),key))
                    if changed.rowcount!=1:raise RuntimeError('Receipt CAS failed')
                local.execute('COMMIT')
            return result
        except BaseException:
            if apply and local.in_transaction:local.execute('ROLLBACK')
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--registry',required=True);p.add_argument('--worker-state',required=True);p.add_argument('--sid',required=True);p.add_argument('--provisioning-stopped',action='store_true');p.add_argument('--apply',action='store_true');a=p.parse_args()
    print(json.dumps(reconcile(a.registry,a.worker_state,a.sid,provisioning_stopped=a.provisioning_stopped,apply=a.apply),sort_keys=True))

if __name__=='__main__':main()
