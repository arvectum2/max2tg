"""Actual proxy-only MAX and Telegram clients under a bound rotation controller."""
import asyncio
import os
from pathlib import Path
import ssl
import subprocess
import time
import sys

from aiohttp import web, WSMsgType
from app.encrypted_credentials import install_encrypted_worker
from app.encrypted_storage import create_key, install_envelope
from app.isolated_transport import isolated_max_client, isolated_telegram_bot
from app.linux_egress_policy import BrokerPins
from app.max_client import OpCode
from app.max_wire import encode, decode
from app.operator_pin_rotation import NamespaceHandle, RotationGrant, frame, request, read_frame_fd, read_receipt
from app.pinned_broker import PinnedBroker
from tests.linux_live_rotation_fixture import candidate
from tests.linux_veth_fixture import IPS, DROP, call, isolated, unprivileged, denied

SID='a'*24


async def marker(path, timeout=12):
    async with asyncio.timeout(timeout):
        while not path.is_file():
            await asyncio.sleep(.05)


async def server(cert, root):
    unprivileged()
    isolated([], {'lo','m2server'})
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert+'/cert.pem',cert+'/key.pem')
    auth=[]; effects=[]; tg=[]; transports=[]
    async def ws(req):
        ip=req.transport.get_extra_info('sockname')[0]
        assert ip in (IPS[0],IPS[2])
        w=web.WebSocketResponse()
        await w.prepare(req)
        authorized=False
        async for msg in w:
            assert msg.type==WSMsgType.BINARY
            pkt=decode(msg.data);op=pkt['opcode'];payload={}
            if op==OpCode.HANDSHAKE:
                assert pkt['payload']['deviceId']=='synthetic-device'
            elif op==OpCode.AUTH_SNAPSHOT:
                assert pkt['payload']['token']=='synthetic-max'
                auth.append(ip);authorized=True
                payload={'profile':{'contact':{'id':101}}}
            elif op==OpCode.SEND_MESSAGE:
                assert authorized and pkt['payload']['chatId']==42
                text=pkt['payload']['message']['text']
                effects.append((ip,text))
                if text=='ambiguous':
                    (root/'effect-observed').touch()
                    # Simulate effect committed but receipt not returned.
                    await asyncio.sleep(12)
                payload={'message':{'id':100+len(effects)}}
            await w.send_bytes(encode({'cmd':1,'seq':pkt['seq'],'opcode':op,'payload':payload}))
        return w

    async def api(req):
        assert req.transport.get_extra_info('sockname')[0]==IPS[1]
        method=req.match_info['method']
        if method=='getMe':
            data={'id':900,'is_bot':True,'first_name':'Synthetic','username':'synthetic_bot'}
        elif method=='getUpdates':
            data=[]
        elif method=='sendMessage':
            form=await req.post()
            assert int(form['chat_id'])==-10001
            tg.append(form['text']);transports.append(req.transport)
            data={'message_id':200+len(tg),'date':1700000000,
                  'chat':{'id':-10001,'type':'supergroup'},'text':form['text']}
        else:
            raise AssertionError('Unexpected Bot API method')
        return web.json_response({'ok':True,'result':data})
    app=web.Application()
    app.router.add_get('/websocket',ws)
    app.router.add_post('/bot123456:synthetic/{method}',api)
    runner=web.AppRunner(app,handler_cancellation=True)
    await runner.setup()
    try:
        for ip in IPS:
            await web.TCPSite(runner,ip,443,ssl_context=ctx).start()
        print('MOCK READY',flush=True)
        await asyncio.to_thread(sys.stdin.buffer.read,1)
        assert auth==[IPS[0],IPS[2]],auth
        assert effects==[(IPS[0],'before'),(IPS[0],'ambiguous'),(IPS[2],'after')],effects
        assert tg==['before','after'],tg
        assert len(transports)==2 and transports[0] is transports[1]
        print('CLIENT EFFECTS PASS: 3 exact MAX effects, 2 exact TG effects, same TG TLS transport',flush=True)
    finally:
        await runner.cleanup()


async def broker(root,expiry,nonce,identity,inherited_fd):
    os.close(inherited_fd)
    stat=os.stat('/proc/self/ns/net')
    assert (stat.st_dev,stat.st_ino)==identity
    unprivileged()
    isolated([],{'lo','eth0'})
    old=BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':expiry})
    new=candidate(old)
    b=PinnedBroker(old,{SID:root/'max','telegram':root/'tg'})
    stop=asyncio.Event()
    task=asyncio.create_task(b.serve(stop))
    try:
        await asyncio.wait_for(b.ready.wait(),2)
        (root/'ready').touch()
        await marker(root/'effect-observed')
        async def update(current,replacement):
            os.write(sys.stdout.fileno(),frame(request(nonce,current,replacement)))
            raw=await asyncio.to_thread(read_frame_fd,sys.stdin.fileno())
            return read_receipt(raw,nonce,current,replacement)
        await b.rotate(new,verified_update=update)
        await denied(IPS[0],443)
        (root/'rotated').touch()
        await marker(root/'done')
        assert b.ready.is_set()
        print('ACTUAL CLIENT ROTATION BROKER PASS',flush=True)
    finally:
        stop.set()
        await asyncio.wait_for(task,3)


async def client(cert,root):
    unprivileged()
    isolated([],{'lo'})
    ctx=ssl.create_default_context(cafile=cert+'/cert.pem')
    # Synthetic pre-provisioned account binding; no real credentials.
    secret=root/'secret';secret.mkdir(mode=0o700)
    keys=root/'keys';keys.mkdir(mode=0o700)
    key=keys/'max.key'
    create_key(key,'max:'+SID)
    install_encrypted_worker(secret,SID,{'session_id':SID,'MAX_TOKEN':'synthetic-max',
        'MAX_DEVICE_ID':'synthetic-device'},key)
    install_envelope(secret/'account.enc',b'101',key,'max:'+SID,'account-binding')
    await marker(root/'ready')
    async with isolated_max_client(root/'max'/'proxy.sock',secret,SID,context=ctx,
                                    key_path=key,require_account_binding=True) as max_client:
        ready=asyncio.Queue()
        @max_client.on_ready
        async def on_ready(_):
            ready.put_nowait(max_client._my_id)
        task=asyncio.create_task(max_client.run())
        try:
            assert await asyncio.wait_for(ready.get(),5)==101
            async with isolated_telegram_bot(root/'tg'/'proxy.sock','123456:synthetic',context=ctx) as bot:
                assert bot.id==900
                receipt=await max_client.send_message(42,'before')
                assert receipt['message']['id']==101
                assert (await bot.send_message(-10001,'before')).message_id==201
                # Do not retry the ambiguous RPC. It has an effect but no receipt.
                result=await asyncio.wait_for(max_client.send_message(42,'ambiguous'),8)
                assert not result, result
                await marker(root/'rotated')
                # Observe retained Telegram transport before the normal MAX
                # reconnect delay can exceed HTTPX's idle keepalive timeout.
                assert (await bot.send_message(-10001,'after')).message_id==202
                assert bot.id==900
                assert await asyncio.wait_for(ready.get(),10)==101
                assert max_client._authorized and max_client._my_id==101
                assert (await max_client.send_message(42,'after'))['message']['id']==103
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    (root/'done').touch()
    print('ACTUAL CLIENT PASS: same MAX account, same Bot object, uncertain send not replayed',flush=True)


def controller(ns,cert,old,root):
    pid=int(ns[2])
    info=os.stat(f'/proc/{pid}/ns/net')
    identity=(info.st_dev,info.st_ino)
    handle=NamespaceHandle(pid,expected=identity)
    grant=RotationGrant(handle,old,candidate(old))
    keeper=subprocess.Popen(['unshare','--net','sleep','45'])
    broker_process=None;client_process=None
    try:
        for _ in range(50):
            if os.readlink('/proc/self/ns/net')!=os.readlink(f'/proc/{keeper.pid}/ns/net'):
                break
            time.sleep(.05)
        else:
            raise AssertionError('Client namespace not created')
        client_ns=['nsenter','-t',str(keeper.pid),'-n','--']
        call(*client_ns,'ip','link','set','lo','up')
        isolated(client_ns,{'lo'})
        assert os.readlink(f'/proc/{keeper.pid}/ns/net')!=os.readlink(f'/proc/{pid}/ns/net')
        broker_process=subprocess.Popen(
            ['/usr/bin/nsenter',f'--net=/proc/self/fd/{handle.fd}','--',
             *DROP,sys.executable,'-m','tests.linux_client_rotation_fixture','broker',
             root,str(old.expires_at),grant.nonce,str(identity[0]),str(identity[1]),str(handle.fd)],
            pass_fds=(handle.fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        client_process=subprocess.Popen([*client_ns,*DROP,sys.executable,'-m',
            'tests.linux_client_rotation_fixture','client',cert,root])
        receipt=grant.execute(read_frame_fd(broker_process.stdout.fileno(),timeout=12))
        out,_=broker_process.communicate(receipt,timeout=20)
        print(out.decode(),flush=True)
        assert broker_process.returncode==0 and b'ACTUAL CLIENT ROTATION BROKER PASS' in out
        assert client_process.wait(timeout=3)==0
    finally:
        try:
            if broker_process is not None and broker_process.poll() is None:
                if not broker_process.stdin.closed:broker_process.stdin.close()
                broker_process.wait(timeout=14)
            if client_process is not None and client_process.poll() is None:
                client_process.wait(timeout=20)
        finally:
            handle.close()
            keeper.terminate()
            keeper.wait(timeout=3)


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='server':
        asyncio.run(server(sys.argv[2],Path(sys.argv[3])))
    elif mode=='broker':
        asyncio.run(broker(Path(sys.argv[2]),int(sys.argv[3]),sys.argv[4],
                           (int(sys.argv[5]),int(sys.argv[6])),int(sys.argv[7])))
    elif mode=='client':
        asyncio.run(client(sys.argv[2],Path(sys.argv[3])))
    else:
        raise ValueError('Unknown fixture action')
