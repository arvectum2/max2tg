"""Disposable operator identity proofs via the actual synthetic TLS broker."""
import asyncio,json,ssl,sys,sqlite3
from pathlib import Path
from contextlib import closing
from app.capsule_registry import CapsuleRegistry
from app.operator_sessions import OperatorSessions
from app.isolated_transport import probe_max_identity
from app.encrypted_credentials import load_encrypted_worker
from app.max_account_binding import bound_viewer
from tests.operator_sessions_fixture import bot
from tests.supervised_proxy_fixture import assert_sandbox


def rows():
    out=[]
    for path,table in (('/registry/inbox.sqlite3','telegram_ingress'),('/registry/telegram-egress.sqlite3','telegram_egress'),('/state/to-telegram.sqlite3','worker_telegram_intents')):
        with closing(sqlite3.connect('file:'+path+'?mode=ro',uri=True)) as db:out.append(sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr))
    return out

async def main():
    assert_sandbox();action,sid=sys.argv[1:];r=CapsuleRegistry('/registry');o=OperatorSessions(r,provisioning_stopped=True,require_account_binding=True)
    try:
        req,actor,forum=r.db.execute('SELECT request_id,actor,forum FROM shared_onboarding WHERE sid=?',(sid,)).fetchone();actor=int(actor);forum=int(forum);i=actor-100;assert i in (0,1) and forum==-10000-i
        args=('/state','/channel','/secret','/keys/worker.key');b=bot(actor,forum);ctx=ssl.create_default_context(cafile='/state/test-ca.pem')
        async def probe(values):return await probe_max_identity('/network/proxy.sock',values,context=ctx)
        if action=='bind':
            assert o.status(req)['state']=='PROVISIONED';await o.bind_account(req,*args,probe=probe);assert bound_viewer(args[2],sid,args[3],required=True)==actor
            assert (await o.activate(req,b,*args))['state']=='ACTIVE';o.onboarding.plane.bind_topic(sid,42,77,actor);print(json.dumps({'bound':True}));return
        assert action=='renew' and i==0
        before=rows();p=Path('/secret/max.enc');old=p.read_bytes();pin=Path('/secret/account.enc').read_bytes();values=load_encrypted_worker(args[2],sid,args[3]);o.pause(req,*args)
        try:await o.reauthenticate(req,*args,dict(values,MAX_TOKEN='synthetic-foreign-0'),probe=probe)
        except PermissionError:pass
        else:raise AssertionError('Foreign account accepted')
        assert p.read_bytes()==old and o.status(req)['state']=='PAUSED'
        candidate=dict(values,MAX_TOKEN='synthetic-renewed-0');await o.reauthenticate(req,*args,candidate,probe=probe)
        assert load_encrypted_worker(args[2],sid,args[3])==candidate and Path('/secret/account.enc').read_bytes()==pin
        assert (await o.activate(req,b,*args))['state']=='ACTIVE';assert rows()==before
        print(json.dumps({'foreign_refused':True,'same_account_renewed':True,'receipts_unchanged':True}))
    finally:o.close();r.close()

if __name__=='__main__':asyncio.run(main())
