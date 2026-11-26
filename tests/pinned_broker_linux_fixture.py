"""Actual numeric IPv4/TLS and expiry, confined to disposable loopback."""
import asyncio,json,os,socket,ssl,subprocess,sys,tempfile,time
from pathlib import Path
from app.linux_egress_policy import BrokerPins
from app.pinned_broker import PinnedBroker

ADDRESSES=('8.8.8.8','9.9.9.9')


def call(*args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=5).stdout


def isolated():
    assert Path('/.dockerenv').is_file() and not Path('/var/run/docker.sock').exists()
    assert {v['ifname'] for v in json.loads(call('ip','-j','link','show')) if 'UP' in v['flags']}=={'lo'}
    assert len(Path('/proc/net/route').read_text().splitlines())==1
    assert not json.loads(call('ip','-j','-6','route','show','default'))
    assert 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()


def setup():
    isolated();assert os.getuid()==0 and 'CapEff:\t0000000000001000' in Path('/proc/self/status').read_text()
    for ip in ADDRESSES:call('ip','address','add',ip+'/32','dev','lo')
    isolated();print('SETUP READY',flush=True);time.sleep(45)


async def request(path,host):
    r,w=await asyncio.open_unix_connection(str(path/'proxy.sock'));w.write(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode());await w.drain();return r,w,await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),2)


async def exercise():
    isolated();assert os.getuid()==65532 and os.getgid()==65532
    status=Path('/proc/self/status').read_text();assert 'CapEff:\t0000000000000000' in status and 'CapPrm:\t0000000000000000' in status
    for ip in ADDRESSES:
        result=json.loads(call('ip','-j','route','get',ip));assert len(result)==1 and result[0]['type']=='local' and result[0]['dev']=='lo'
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain('/cert/cert.pem','/cert/key.pem')
    client_context=ssl.create_default_context(cafile='/cert/cert.pem');assert client_context.check_hostname and client_context.verify_mode==ssl.CERT_REQUIRED
    peers=[]
    async def echo(r,w):
        peers.append(w.get_extra_info('sockname')[0])
        try:
            while data:=await r.read(128):w.write(data);await w.drain()
        finally:
            w.close()
            try:await w.wait_closed()
            except OSError:pass
    listeners=[await asyncio.start_server(echo,ip,443,ssl=server_context,family=socket.AF_INET) for ip in ADDRESSES]
    with tempfile.TemporaryDirectory(prefix='pins-') as d:
        channels={k:Path(d)/str(i) for i,k in enumerate(('a'*24,'b'*24,'telegram'))}
        for p in channels.values():p.mkdir(mode=0o700)
        pins=BrokerPins.parse({'max-text':[ADDRESSES[0]],'telegram':[ADDRESSES[1]],'expires_at':int(time.time())+8});broker=PinnedBroker(pins,channels);stop=asyncio.Event();task=asyncio.create_task(broker.serve(stop));writers=[];readers=[]
        try:
            await asyncio.wait_for(broker.ready.wait(),2)
            for role,host in (('a'*24,'api.telegram.org'),('telegram','api.oneme.ru')):
                _,w,head=await request(channels[role],host);writers.append(w);assert b'403' in head
            assert not peers
            for role,host in (('a'*24,'api.oneme.ru'),('b'*24,'api.oneme.ru'),('telegram','api.telegram.org')):
                r,w,head=await request(channels[role],host);writers.append(w);readers.append(r);assert b'200' in head
                await w.start_tls(client_context,server_hostname=host,ssl_handshake_timeout=2);assert w.get_extra_info('ssl_object') is not None
                w.write(b'confirmed');await w.drain();assert await asyncio.wait_for(r.readexactly(9),2)==b'confirmed'
            assert peers==[ADDRESSES[0],ADDRESSES[0],ADDRESSES[1]]
            assert await asyncio.wait_for(task,10)=='EXPIRED'
            for r in readers:
                try:assert await asyncio.wait_for(r.read(),2)==b''
                except (ConnectionResetError,ssl.SSLEOFError):pass
            assert not broker.ready.is_set() and not any(s.tasks for s in broker.servers.values()) and not any((p/'proxy.sock').exists() for p in channels.values())
            print('PINNED NUMERIC TLS PASS '+json.dumps({'actual_numeric_tls_connections':3,'foreign_roles_denied':2,'expired_tunnels_closed':3,'broker_effective_capabilities':0,'external_interfaces':0},sort_keys=True),flush=True)
        finally:
            stop.set();await asyncio.gather(task,return_exceptions=True)
            for w in writers:
                w.close()
                try:await asyncio.wait_for(w.wait_closed(),1)
                except (OSError,TimeoutError):pass
            for listener in listeners:listener.close();await listener.wait_closed()

if __name__=='__main__':
    if sys.argv[1]=='setup':setup()
    elif sys.argv[1]=='exercise':asyncio.run(exercise())
    else:raise ValueError('Unknown test action')
