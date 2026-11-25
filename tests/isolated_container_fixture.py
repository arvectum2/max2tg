"""Synthetic Docker fixture; never accesses real accounts or host services."""
from __future__ import annotations
import asyncio,json,os,sys,time,socket,fcntl,struct
from pathlib import Path
from types import SimpleNamespace
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.worker_provision import prepare_worker,check_worker
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.max_client import MaxMessage
from app.worker_secrets import install_worker_secret,load_worker_secret,build_max_client

UID=65532

async def _prepare(*,defer_second=False):
    for name in ('registry','channel0','channel1','state0','state1','secret0','secret1'):
        p=Path('/'+name);assert p.is_dir() and not list(p.iterdir());p.chmod(0o700)
    r=CapsuleRegistry('/registry');onboarding=SharedOnboarding(r);sessions=[]
    for i in range(2):
        actor=100+i;forum=-10000-i;req=f'{i+1:024x}';sid=onboarding.reserve(req,actor,forum)
        class VerifyBot:
            async def get_me(self):return SimpleNamespace(id=900)
            async def get_chat(self,gid):return SimpleNamespace(id=gid,type='supergroup',is_forum=True)
            async def get_chat_member(self,gid,uid):return SimpleNamespace(user=SimpleNamespace(id=uid),status='creator' if uid==actor else 'administrator',can_manage_topics=True)
            async def get_chat_member_count(self,gid):return 2
        bot=VerifyBot();assert await onboarding.verify_forum(req,bot)=='FORUM_VERIFIED'
        install_worker_secret(f'/secret{i}',sid,{'session_id':sid,'MAX_TOKEN':f'synthetic-secret-{i}','MAX_DEVICE_ID':f'synthetic-{i}'})
        key=onboarding.ipc_key(req).read_bytes();folders=(f'/state{i}',f'/channel{i}',f'/secret{i}')
        assert prepare_worker(*folders,sid,actor,forum,key)
        assert prepare_worker(*folders,sid,actor,forum,key) # idempotent local provisioning
        def check(s,a,f,k):return check_worker(*folders,s,a,f,k.read_bytes())
        onboarding.confirm_provisioned(req,check=check);assert not r.get(sid)['desired_active']
        if not (defer_second and i==1):
            assert await onboarding.activate(req,bot,check=check)==sid
            onboarding.plane.bind_topic(sid,42,77,actor)
        sessions.append([sid,actor,forum])
    (r.root/'synthetic-bot-secret').write_text('not-a-real-token');(r.root/'synthetic-bot-secret').chmod(0o600)
    r.close()
    for name in ('registry','channel0','channel1','state0','state1','secret0','secret1'):
        p=Path('/'+name);os.chown(p,UID,UID)
        for q in p.rglob('*'):os.chown(q,UID,UID)
    print(json.dumps(sessions),flush=True)


def prepare(*,defer_second=False):asyncio.run(_prepare(defer_second=defer_second))


def assert_sandbox():
    assert os.getuid()==UID and 'TG_BOT_TOKEN' not in os.environ
    status=Path('/proc/self/status').read_text();assert 'CapEff:\t0000000000000000' in status and 'NoNewPrivs:\t1' in status
    assert not Path('/var/run/docker.sock').exists()
    # Docker network=none gives only loopback.
    # Linux may expose down kernel tunnel interfaces even with network=none.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        up={name for _,name in socket.if_nameindex() if struct.unpack_from('H',fcntl.ioctl(sock.fileno(),0x8913,struct.pack('16s24x',name.encode())),16)[0]&1}
    assert up=={'lo'},up
    assert len(Path('/proc/net/route').read_text().splitlines())==1

class FakeBot:
    def __init__(self,sessions):self.sessions=sessions;self.sent=[]
    async def get_me(self):return SimpleNamespace(id=900)
    async def get_updates(self,**kw):
        if kw['offset']:return []
        return [{'update_id':i+1,'message':{'message_id':100+i,'from':{'id':a},'chat':{'id':f},'message_thread_id':77,'text':f'incoming-{a}'}} for i,(_,a,f) in enumerate(self.sessions)]
    async def send_message(self,**kw):
        match=next((x for x in self.sessions if x[2]==kw['chat_id']),None);assert match and kw['message_thread_id']==77 and kw['text']==f'outgoing-{match[1]}'
        self.sent.append(kw['chat_id']);return SimpleNamespace(message_id=5000+len(self.sent),chat=SimpleNamespace(id=kw['chat_id']),message_thread_id=77)

async def gateway(sessions):
    assert_sandbox();assert not any(Path(p).exists() for p in ('/state0','/state1','/secret0','/secret1'))
    r=CapsuleRegistry('/registry');assert all(x['credential_mode']=='external' for x in r.list_capsules())
    assert not list(r.root.rglob('credentials.json')) and not list(r.root.rglob('max.json'))
    assert all(b'synthetic-secret-' not in p.read_bytes() for p in r.root.rglob('*') if p.is_file())
    b=FakeBot(sessions);g=SharedGatewayRuntime(r,b,channels={sid:Path(f'/channel{i}') for i,(sid,_,_) in enumerate(sessions)},external_only=True)
    try:
        await g.start();print('READY',flush=True);await g.poll_once();deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            await g.drain_once()
            if g.egress.counts()=={'SENT':2} and g.inbox.counts()=={'ACKED':2} and all(Path(f'/channel{i}/done').is_file() for i in range(2)):break
            await asyncio.sleep(.05)
        assert g.egress.counts()=={'SENT':2} and g.inbox.counts()=={'ACKED':2};assert sorted(b.sent)==sorted(x[2] for x in sessions);assert all(Path(f'/channel{i}/done').is_file() for i in range(2));print('GATEWAY PASS: 2 exact routes, 2 ACKs, 0 duplicates',flush=True)
    finally:await g.close();r.close()

class FakeMax:
    _authorized=True
    def __init__(self,actor):self.actor=actor
    def on_message(self,cb):self.callback=cb
    async def telegram_echo_tg_id(self,msg):return None
    async def send_message(self,chat_id,text,**kw):
        assert chat_id==42 and text==f'incoming-{self.actor}';return {'message':{'id':'12345'}}

async def worker(sid,actor,forum):
    assert_sandbox()
    for p in ('/registry','/channel0','/channel1','/state0','/state1','/secret0','/secret1'):
        assert not Path(p).exists(),p
    secret=load_worker_secret('/secret',sid);assert secret['MAX_TOKEN']==f'synthetic-secret-{actor-100}'
    try:Path('/secret/max.json').write_text('must not write')
    except OSError:pass
    else:raise AssertionError('Secret mount is writable')
    assert not hasattr(sys.modules.get('app.isolated_worker_runtime'),'registry')
    def factory(*,token,device_id,debug):
        assert token==f'synthetic-secret-{actor-100}' and device_id==f'synthetic-{actor-100}' and debug is False
        return FakeMax(actor)
    m=build_max_client('/secret',sid,factory=factory);w=IsolatedWorkerRuntime(sid,'/state','/channel','/state/key',forum,actor,m)
    try:
        await w.start();assert await w.on_message(MaxMessage(chat_id=42,message_id='123',text=f'outgoing-{actor}'))=='PENDING'
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            await w.drain_once()
            if w.egress.counts()=={'SENT':1} and w.ledger.counts()=={'SENT':1}:break
            await asyncio.sleep(.05)
        assert w.egress.counts()=={'SENT':1} and w.ledger.counts()=={'SENT':1};assert w.topics.tg_for_max_message(42,'123')>0
        Path('/channel/done').write_text('synthetic success');print('WORKER PASS: namespace, own read-only secret, signed route, 1 SENT each direction',flush=True)
    finally:await w.close()

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='init':prepare()
    elif mode=='gateway':asyncio.run(gateway(json.loads(sys.argv[2])))
    elif mode=='worker':asyncio.run(worker(sys.argv[2],int(sys.argv[3]),int(sys.argv[4])))
    else:raise SystemExit('fixture mode required')
