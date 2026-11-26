"""Reconstruct disposable runtime from an authenticated checkpoint, offline."""
import asyncio,json,sys
from pathlib import Path
from app.coordinated_backup import GATEWAY,WORKER
from app.recovery_manifest import RecoveryAnchor,_private_dir,_file
from app.worker_provision import _ensure
from app.restored_start_gate import launch_verified_restore
from app.encrypted_credentials import load_encrypted_bot
from tests.supervised_proxy_fixture import assert_sandbox


def main():
    assert_sandbox();sids=json.loads(sys.argv[2]);assert len(sids)==2 and len(set(sids))==2
    registry=Path('/newregistry');anchor=RecoveryAnchor('/anchor');snapshot=Path('/restores/snapshot-restore')
    workers={sid:(Path('/newstate'+str(i)),Path('/newchannel'+str(i)),Path('/secret'+str(i))) for i,sid in enumerate(sids)}
    keys={sid:Path('/keys'+str(i)+'/worker.key') for i,sid in enumerate(sids)}
    if sys.argv[1]=='prepare':
        anchor.verify(snapshot,quiescent=True)
        for p in [registry,*[v for state,channel,_ in workers.values() for v in (state,channel)]]:
            assert not any(_private_dir(p).iterdir())
        for n in GATEWAY:_ensure(registry/n,_file(snapshot,'gateway-'+n).read_bytes())
        for n in ('shared-gateway.lock','shared-provisioning.lock'):_ensure(registry/n,b'')
        _ensure(registry/'test-ca.pem',_file(Path('/oldregistry'),'test-ca.pem').read_bytes())
        (registry/'tenants').mkdir(mode=0o700)
        for i,(sid,(state,channel,secret)) in enumerate(workers.items()):
            for n in WORKER:_ensure(state/n,_file(snapshot,sid+'-'+n).read_bytes())
            for n in ('key','test-ca.pem'):_ensure(state/n,_file(Path('/oldstate'+str(i)),n).read_bytes())
            _ensure(state/'worker.lock',b'');cap=registry/'tenants'/sid;cap.mkdir(mode=0o700)
            _ensure(cap/'telegram-ipc.key',_file(Path('/oldregistry/tenants')/sid,'telegram-ipc.key').read_bytes())
        print(json.dumps({'prepared':True,'dirty':False}));return
    assert sys.argv[1]=='gate'
    async def authorized():
        assert anchor._load()['dirty'] is True
        return {'verified':True,'dirty':True,'live_launch_authorized':False}
    load_encrypted_bot('/botsecret',123456,'/botkeys/bot.key')
    try:result=asyncio.run(launch_verified_restore(anchor,registry,workers,start=authorized,secret_keys=keys))
    except PermissionError:
        if anchor._load()['dirty'] is True:print(json.dumps({'verified':False,'dirty':True}));return
        raise
    print(json.dumps(result))

if __name__=='__main__':main()
