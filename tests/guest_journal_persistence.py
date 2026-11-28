"""Synthetic two-phase journal test, optionally across a real guest reboot.

No messenger clients or external network. The synthetic effect is an
exclusive fsynced local marker. Only a newly-created private test directory.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from app.capsule_registry import CapsuleRegistry
from app.egress_policy import EgressDenied
from app.encrypted_storage import _read, _write
from app.generation_ledger import require
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_inbox import TelegramInbox
from app.tg_outbox import TelegramOutbound

GROUP=-100001
ACTOR=111
BEFORE=([(1,'ACKED'),(2,'INFLIGHT'),(3,'RECEIVED')],
        [(1,'SENT','1001'),(2,'SENDING',None),(3,'RECEIVED',None)])
AFTER=([(1,'ACKED'),(2,'UNKNOWN'),(3,'ACKED')],
       [(1,'SENT','1001'),(2,'UNKNOWN',None),(3,'SENT','1003')])


def boot():
    if sys.platform=='linux':
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return 'process-only'


def update(n):
    return dict(update_id=n,message={'chat':{'id':GROUP},'from':{'id':ACTOR},'text':f'synthetic-{n}'})


def rows(root):
    def query(path,sql):
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
            return db.execute(sql).fetchall()
    return (query(root/'registry'/'inbox.sqlite3','SELECT update_id,status FROM telegram_ingress ORDER BY update_id'),
            query(root/'worker'/'outbound.sqlite3','SELECT telegram_id,status,max_id FROM outbound ORDER BY telegram_id'))


class SyntheticClient:
    def __init__(self,root,crash):
        self.root=root
        self.crash=crash

    async def send_message(self,chat,text,**kwargs):
        n=kwargs['telegram_message_id']
        assert chat==42 and text==f'synthetic-{n}'
        _write(self.root/'effects'/str(n),b'effect')
        if self.crash and n==2:os._exit(17)
        return {'message':{'id':1000+n}}


def stores(root):
    registry=CapsuleRegistry(root/'registry')
    plane=TelegramControlPlane(registry)
    inbox=TelegramInbox(registry.root/'inbox.sqlite3',plane)
    worker=TelegramOutbound(root/'worker'/'outbound.sqlite3')
    return registry,plane,inbox,worker


async def seed(root):
    require(root.is_dir() and {p.name for p in root.iterdir()}=={'effects'})
    reg,plane,inbox,worker=stores(root)
    user=reg.create_account()
    sid=reg.create_capsule(user)
    reg.db.execute('INSERT INTO tg_owners VALUES(?,?)',(str(ACTOR),user))
    reg.db.execute('UPDATE capsules SET desired_active=1 WHERE session_id=?',(sid,))
    reg.db.execute('INSERT INTO tg_routes VALUES(?,?,?,?,?)',(str(GROUP),sid,user,'ACTIVE',1.0))
    client=SyntheticClient(root,True)
    for n in (1,2,3):
        assert inbox.ingest(update(n))==(n,sid)
        assert worker.enqueue(GROUP,n,42,{'text':f'synthetic-{n}','elements':[],'link':None})=='RECEIVED'
    async def deliver(session,event):
        assert session==sid
        return await worker.deliver_one(client,GROUP,event['update_id'])=='SENT'
    assert await inbox.dispatch(1,deliver)=='ACKED'
    await inbox.dispatch(2,deliver)
    raise AssertionError('Expected abrupt process exit')


def prepare(root):
    root=Path(root).absolute()
    require(root.name.startswith('max2tg-journal-') and not root.exists() and not root.is_symlink())
    root.mkdir(mode=0o700)
    (root/'effects').mkdir(mode=0o700)
    p=subprocess.run([sys.executable,'-m','tests.guest_journal_persistence','seed',str(root)],timeout=15)
    assert p.returncode==17
    assert rows(root)==BEFORE and {p.name for p in (root/'effects').iterdir()}=={'1','2'}
    _write(root/'prepared.json',json.dumps({'version':1,'boot':boot(),'state':'PREPARED'},sort_keys=True).encode())
    print('JOURNAL PREPARED PASS: ACKED/SENT, INFLIGHT/SENDING, RECEIVED; two effects',flush=True)


async def recover(root,*,require_reboot):
    root=Path(root).absolute()
    require(root.name.startswith('max2tg-journal-') and root.is_dir() and not root.is_symlink())
    before=json.loads(_read(root/'prepared.json',1024))
    require(type(before) is dict and set(before)=={'version','boot','state'}
            and type(before['version']) is int and before['version']==1 and before['state']=='PREPARED')
    current=boot()
    if require_reboot:require(sys.platform=='linux' and before['boot']!=current)
    require(not (root/'recovery.json').exists() and not (root/'recovery.json').is_symlink())
    require(rows(root)==BEFORE and {p.name for p in (root/'effects').iterdir()}=={'1','2'})
    _write(root/'recovery.json',b'{"state":"RECOVERING"}')
    reg,plane,inbox,worker=stores(root)
    try:
        sid=plane.route_group(GROUP,ACTOR)
        assert sid and inbox.ready()==[3]
        assert inbox.counts()=={'ACKED':1,'UNKNOWN':1,'RECEIVED':1}
        assert worker.counts()=={'SENT':1,'UNKNOWN':1,'RECEIVED':1}
        client=SyntheticClient(root,False)
        async def deliver(session,event):
            assert session==sid
            return await worker.deliver_one(client,GROUP,event['update_id'])=='SENT'
        for n,expected in ((1,'ACKED'),(2,'UNKNOWN')):
            assert await inbox.dispatch(n,deliver)==expected
            assert await worker.deliver_one(client,GROUP,n)==('SENT' if n==1 else 'UNKNOWN')
        assert await inbox.dispatch(3,deliver)=='ACKED'
        assert not inbox.ready()
    finally:
        worker.close();inbox.close();reg.close()
    assert rows(root)==AFTER and {p.name for p in (root/'effects').iterdir()}=={'1','2','3'}
    result={'state':'VERIFIED','boot_changed':before['boot']!=current,
            'effects':3,'duplicates':0,'live_launch_authorized':False}
    _write(root/'recovery.json',json.dumps(result,sort_keys=True).encode(),replace=True)
    print('JOURNAL RECOVERY PASS '+json.dumps(result,sort_keys=True),flush=True)
    return result


if __name__=='__main__':
    mode=sys.argv[1]
    root=Path(sys.argv[2]).absolute()
    if mode=='seed':asyncio.run(seed(root))
    elif mode=='prepare':prepare(root)
    elif mode=='verify':asyncio.run(recover(root,require_reboot=True))
    elif mode=='verify-process-only':asyncio.run(recover(root,require_reboot=False))
    else:raise EgressDenied('Unknown synthetic fixture mode')
