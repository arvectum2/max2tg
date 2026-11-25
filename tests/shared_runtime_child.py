"""Process fixture only: no network, no real credentials or bot."""
import asyncio,sys,os
from app.capsule_registry import CapsuleRegistry
from app.shared_worker_runtime import SharedWorkerRuntime
from app.max_client import MaxMessage

class SyntheticMax:
    _authorized=True
    def on_message(self,callback):self.callback=callback
    async def telegram_echo_tg_id(self,msg):return None
    async def send_message(self,chat_id,text,**kw):
        assert chat_id==42 and kw['telegram_message_id']>0
        return {'message':{'id':str(100000+kw['telegram_message_id'])}}

async def main():
    assert 'TG_BOT_TOKEN' not in os.environ
    r=CapsuleRegistry(sys.argv[1]);sid=sys.argv[2];m=SyntheticMax();w=SharedWorkerRuntime(r,sid,m)
    try:
        await w.start();await m.callback(MaxMessage(chat_id=42,message_id='123',text='synthetic '+sid));print('READY',flush=True)
        while True:await w.drain_once();await asyncio.sleep(.05)
    finally:await w.close();r.close()

if __name__=='__main__':asyncio.run(main())
