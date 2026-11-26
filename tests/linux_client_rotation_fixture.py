"""Actual proxy-only MAX and Telegram clients under a bound rotation controller."""
import asyncio
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
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
from app.rotation_fence import RotationFence
from tests.linux_live_rotation_fixture import candidate
from tests.linux_veth_fixture import IPS, DROP, call, isolated, unprivileged, denied

SID='a'*24


async def marker(path, timeout=12):
    async with asyncio.timeout(timeout):
        while not path.is_file():
            await asyncio.sleep(.05)


async def server(cert, root, journals=False, crash=False):
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
            if journals and op==OpCode.AUTH_SNAPSHOT:
                # Replayed pre-rotation event on reconnect is deliberately identical.
                for i,text in ([(1,'before')] if ip==IPS[0] else [(1,'before'),(2,'after')]):
                    await w.send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,
                        'payload':{'chatId':42,'message':{'id':300+i,'sender':999,'text':text}}}))
        return w

    async def api(req):
        assert req.transport.get_extra_info('sockname')[0]==IPS[1]
        method=req.match_info['method']
        if method=='getMe':
            data={'id':900,'is_bot':True,'first_name':'Synthetic','username':'synthetic_bot'}
        elif journals and method=='getChat':
            data={'id':-10001,'type':'supergroup','title':'Synthetic','is_forum':True,
                  'accent_color_id':0,'max_reaction_count':1,'accepted_gift_types':{
                      'unlimited_gifts':False,'limited_gifts':False,'unique_gifts':False,
                      'premium_subscription':False,'gifts_from_channels':False}}
        elif journals and method=='getChatMemberCount':
            data=2
        elif journals and method=='getChatMember':
            form=await req.post();uid=int(form['user_id'])
            data={'user':{'id':uid,'is_bot':uid==900,'first_name':'Synthetic'},
                  'status':'creator' if uid==100 else 'administrator','is_anonymous':False}
            if uid==900:
                data.update({k:True for k in ('can_be_edited','can_manage_chat','can_delete_messages','can_manage_video_chats','can_restrict_members','can_promote_members','can_change_info','can_invite_users','can_post_stories','can_edit_stories','can_delete_stories','can_manage_topics')})
        elif method=='getUpdates':
            data=[]
            if journals:
                form=await req.post()
                for i,text in enumerate(('before','ambiguous','after'),1):
                    if (root/f'phase{i}').exists() and i>=int(form.get('offset',0)):
                        data.append({'update_id':i,'message':{'message_id':100+i,'date':1700000000,
                            'from':{'id':100,'is_bot':False,'first_name':'Synthetic'},
                            'chat':{'id':-10001,'type':'supergroup','is_forum':True},
                            'message_thread_id':77,'text':text,'is_topic_message':True}})
        elif method=='sendMessage':
            form=await req.post()
            assert int(form['chat_id'])==-10001
            if journals:assert int(form['message_thread_id'])==77
            tg.append(form['text']);transports.append(req.transport)
            data={'message_id':200+len(tg),'date':1700000000,
                  'chat':{'id':-10001,'type':'supergroup'},'text':form['text']}
            if journals:data['message_thread_id']=77
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
        assert auth==([IPS[0]] if crash else [IPS[0],IPS[2]]),auth
        expected=[(IPS[0],'before'),(IPS[0],'ambiguous')]
        if not crash:expected.append((IPS[2],'after'))
        assert effects==expected,effects
        assert tg==(['before'] if crash else ['before','after']),tg
        if not journals:assert len(transports)==2 and transports[0] is transports[1]
        print('CLIENT EFFECTS PASS:',len(effects),'exact MAX effects,',len(tg),'exact TG effects',flush=True)
    finally:
        await runner.cleanup()


async def broker(root,expiry,nonce,identity,inherited_fd,journals=False,crash=False):
    os.close(inherited_fd)
    stat=os.stat('/proc/self/ns/net')
    assert (stat.st_dev,stat.st_ino)==identity
    unprivileged()
    isolated([],{'lo','eth0'})
    old=BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':expiry})
    new=candidate(old)
    if journals:await marker(root/'sid')
    sid=(root/'sid').read_text() if journals else SID
    b=PinnedBroker(old,{sid:root/'max','telegram':root/'tg'})
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
        if crash:
            from app.egress_policy import EgressDenied
            try:
                await b.rotate(new,verified_update=update)
            except EgressDenied as exc:
                assert str(exc)=='Rotation channel closed',str(exc)
            else:raise AssertionError('Rotation accepted without controller receipt')
            assert await asyncio.wait_for(task,3)=='ROTATION_FAILED'
            assert not b.ready.is_set() and not b._accepting
            assert not b._streams
            assert not any((root/name/'proxy.sock').exists() for name in ('max','tg'))
            await denied(IPS[0],443)
            (root/'broker-failed').touch()
            return
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


def controller(ns,cert,old,root,journals=False):
    pid=int(ns[2])
    info=os.stat(f'/proc/{pid}/ns/net')
    identity=(info.st_dev,info.st_ino)
    handle=NamespaceHandle(pid,expected=identity)
    anchor=None;fence=None
    keeper=subprocess.Popen(['unshare','--net','sleep','45'])
    broker_process=None;client_process=None
    try:
        if journals:
            anchor=tempfile.TemporaryDirectory(prefix='rotation-fence-')
            fence=RotationFence(anchor.name,handle)
            nonce=fence.arm(old,candidate(old))
            execute=fence.execute
        else:
            grant=RotationGrant(handle,old,candidate(old))
            nonce=grant.nonce
            execute=grant.execute
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
             root,str(old.expires_at),nonce,str(identity[0]),str(identity[1]),str(handle.fd),*(['journals'] if journals else [])],
            pass_fds=(handle.fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        client_process=subprocess.Popen([*client_ns,*DROP,sys.executable,'-m',
            'tests.linux_client_rotation_fixture','client',cert,root,*(['journals'] if journals else [])])
        receipt=execute(read_frame_fd(broker_process.stdout.fileno(),timeout=20 if journals else 12))
        out,_=broker_process.communicate(receipt,timeout=25 if journals else 20)
        print(out.decode(),flush=True)
        assert broker_process.returncode==0 and b'ACTUAL CLIENT ROTATION BROKER PASS' in out
        assert client_process.wait(timeout=3)==0
        if journals:
            fence.close();fence=None
            with RotationFence(anchor.name,handle) as recovered:
                assert recovered.inspect()['state']=='KERNEL_VERIFIED'
                assert recovered.inspect()['matches']==['replacement']
            print('DURABLE CONTROLLER FENCE PASS',flush=True)
    finally:
        try:
            if broker_process is not None and broker_process.poll() is None:
                if not broker_process.stdin.closed:broker_process.stdin.close()
                broker_process.wait(timeout=14)
            if client_process is not None and client_process.poll() is None:
                client_process.wait(timeout=20)
        finally:
            if fence is not None:fence.close()
            if anchor is not None:anchor.cleanup()
            handle.close()
            keeper.terminate()
            keeper.wait(timeout=3)


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='server':
        asyncio.run(server(sys.argv[2],Path(sys.argv[3]),'journals' in sys.argv[4:],'crash' in sys.argv[4:]))
    elif mode=='broker':
        asyncio.run(broker(Path(sys.argv[2]),int(sys.argv[3]),sys.argv[4],
                           (int(sys.argv[5]),int(sys.argv[6])),int(sys.argv[7]),'journals' in sys.argv[8:],'crash' in sys.argv[8:]))
    elif mode=='client':
        if 'journals' in sys.argv[4:]:
            from tests.linux_journal_rotation_fixture import client as journal_client
            asyncio.run(journal_client(sys.argv[2],Path(sys.argv[3]),crash='crash' in sys.argv[4:]))
        else:asyncio.run(client(sys.argv[2],Path(sys.argv[3])))
    else:
        raise ValueError('Unknown fixture action')
