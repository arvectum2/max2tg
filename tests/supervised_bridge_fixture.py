"""Network-disabled supervised bridge with fake MAX/Bot API boundaries."""
import asyncio,json,os,signal,sys
from pathlib import Path
from types import SimpleNamespace
from tests.isolated_container_fixture import prepare,assert_sandbox
from app.capsule_registry import CapsuleRegistry
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.worker_secrets import build_max_client
from app.max_client import MaxMessage


def atomic(path,data):
    p=Path(path);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data));tmp.replace(p)

class Bot:
    def __init__(self,sessions):self.sessions=sessions;self.calls=[]
    async def get_me(self):return SimpleNamespace(id=900)
    async def get_updates(self,**kw):
        count=2 if Path('/registry/phase2').exists() else 1
        rows=[{'update_id':2*batch+i+1,'message':{'message_id':100+2*batch+i,'from':{'id':actor},'chat':{'id':forum},'message_thread_id':77,'text':f'incoming-{actor}-{batch}'}} for batch in range(count) for i,(_,actor,forum) in enumerate(self.sessions) if 2*batch+i+1>=kw['offset']]
        if Path('/registry/phase3').exists() and kw['offset']<=5:
            _,actor,forum=self.sessions[0];rows.append({'update_id':5,'message':{'message_id':104,'from':{'id':actor},'chat':{'id':forum},'message_thread_id':77,'text':f'incoming-{actor}-ambiguous'}})
        return rows
    async def send_message(self,**kw):
        match=next(x for x in self.sessions if x[2]==kw['chat_id']);assert kw['message_thread_id']==77 and kw['text']==f'outgoing-{match[1]}'
        self.calls.append(kw['chat_id']);return SimpleNamespace(message_id=5000+len(self.calls),chat=SimpleNamespace(id=kw['chat_id']),message_thread_id=77)

async def gateway(sessions):
    assert_sandbox();r=CapsuleRegistry('/registry');b=Bot(sessions);g=SharedGatewayRuntime(r,b,channels={sid:Path(f'/channel{i}') for i,(sid,_,_) in enumerate(sessions)},external_only=True)
    try:
        await g.start();print('READY',flush=True)
        while not Path('/registry/stop').exists():
            await g.poll_once();await g.drain_once()
            atomic('/registry/progress.json',{'ingress':g.inbox.counts(),'egress':g.egress.counts(),'rows':{str(i):g.inbox.row(i)['status'] for i in range(1,6) if g.inbox.row(i)},'bot_calls':b.calls});await asyncio.sleep(.1)
    finally:await g.close();r.close()

class Max:
    _authorized=True
    def __init__(self,actor):self.actor=actor
    def on_message(self,cb):self.callback=cb
    async def telegram_echo_tg_id(self,msg):return None
    async def send_message(self,chat_id,text,**kw):
        assert chat_id==42 and text in (f'incoming-{self.actor}-0',f'incoming-{self.actor}-1',f'incoming-{self.actor}-ambiguous')
        with open('/state/calls','a') as f:f.write(text+'\n');f.flush();os.fsync(f.fileno())
        if text.endswith('-ambiguous'):os._exit(18)
        return {'message':{'id':str(100000+kw['telegram_message_id'])}}

async def worker(sid):
    assert_sandbox();assert not Path('/registry').exists()
    cfg=json.loads(Path('/state/worker-acl.json').read_text());assert cfg['sid']==sid;actor=cfg['actor'];forum=cfg['forum']
    def factory(*,token,device_id,debug):
        assert token==f'synthetic-secret-{actor-100}' and device_id==f'synthetic-{actor-100}' and debug is False;return Max(actor)
    w=IsolatedWorkerRuntime(sid,'/state','/channel','/state/key',forum,actor,build_max_client('/secret',sid,factory=factory));stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
    heartbeat=Path('/state/heartbeat');heartbeat.unlink(missing_ok=True)
    try:
        await w.start();await w.on_message(MaxMessage(chat_id=42,message_id='123',text=f'outgoing-{actor}'))
        while not stop.is_set():
            if Path('/state/crash').exists():Path('/state/crash').unlink();os._exit(17)
            result=await w.drain_once()
            if 'ROUTE_UNAVAILABLE' not in result:heartbeat.touch()
            atomic('/state/progress.json',{'egress':w.egress.counts(),'ingress':w.ledger.counts(),'mapping':w.topics.tg_for_max_message(42,'123')});await asyncio.sleep(.1)
    finally:await w.close()

if __name__=='__main__':
    if sys.argv[1]=='init':
        # Existing fixture provisions seven fresh volumes, then add public ACL.
        import contextlib,io
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):prepare()
        sessions=json.loads(buf.getvalue())
        print(json.dumps(sessions))
    elif sys.argv[1]=='gateway':asyncio.run(gateway(json.loads(sys.argv[2])))
    else:asyncio.run(worker(sys.argv[1]))
