"""Experimental diagnostic; not an accepted routed deployment fixture."""
import asyncio,json,os,ssl,sys,time,subprocess,ipaddress,tempfile
from pathlib import Path
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import verify_ruleset
from app.pinned_broker import PinnedBroker
from tests.pinned_broker_linux_fixture import request

IPS=('8.8.8.8','9.9.9.9','1.1.1.1')

def call(*args,data=None):return subprocess.run(args,input=data,check=True,text=True,capture_output=True,timeout=5).stdout

def isolated(root):
    assert Path('/.dockerenv').is_file() and not Path('/var/run/docker.sock').exists()
    status=Path('/proc/self/status').read_text();assert 'NoNewPrivs:\t1' in status
    assert ('CapEff:\t0000000000001000' if root else 'CapEff:\t0000000000000000') in status
    assert os.getuid()==(0 if root else 65532)
    assert {v['ifname'] for v in json.loads(call('ip','-j','link','show')) if 'UP' in v['flags']}=={'lo','eth0'}
    for family in ('-4','-6'):assert not json.loads(call('ip','-j',family,'route','show','default'))

def pin(expiry):return BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':int(expiry)})

async def server():
    isolated(True)
    for ip in IPS:call('ip','address','add',ip+'/32','dev','eth0')
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/cert/cert.pem','/cert/key.pem')
    async def echo(r,w):
        try:
            while data:=await r.read(128):w.write(data);await w.drain()
        finally:w.close()
    listeners=[await asyncio.start_server(echo,ip,443,ssl=ctx) for ip in IPS]
    print('SERVER READY',flush=True)
    try:await asyncio.sleep(60)
    finally:
        for s in listeners:s.close();await s.wait_closed()

def prepare(peer,expiry):
    isolated(True);assert ipaddress.ip_address(peer).is_private;print('ROUTE PRECHECK',flush=True)
    initial=json.loads(call('nft','-j','list','ruleset'))['nftables'][1:]
    if initial:
        # Docker's private embedded-DNS NAT table is fixture-only; never flush a host ruleset.
        tables=[(x['table']['family'],x['table']['name']) for x in initial if 'table' in x]
        chains={x['chain']['name'] for x in initial if 'chain' in x}
        assert tables==[('ip','nat')] and chains=={'OUTPUT','POSTROUTING','DOCKER_OUTPUT','DOCKER_POSTROUTING'}
        assert all(len(x)==1 and (('table' in x) or (next(iter(x)) in ('chain','rule') and next(iter(x.values()))['family']=='ip' and next(iter(x.values()))['table']=='nat')) for x in initial)
        call('nft','delete','table','ip','nat');print('FIXTURE DNS NAT REMOVED',flush=True)
    assert not json.loads(call('nft','-j','list','ruleset'))['nftables'][1:]
    for ip in IPS:call('ip','route','add',ip+'/32','via',peer,'dev','eth0')
    isolated(True);print('ROUTES READY',flush=True);p=pin(expiry);rules=p.render();call('nft','-c','-f','-',data=rules);print('NFT CHECK READY',flush=True);call('nft','-f','-',data=rules);print('NFT INSTALLED',flush=True)
    assert verify_ruleset(p,json.loads(call('nft','-j','list','ruleset')))['verified'];print('GATE READY',flush=True);time.sleep(40)

async def exercise(expiry):
    isolated(False);ctx=ssl.create_default_context(cafile='/cert/cert.pem');p=pin(expiry)
    for ip,port in ((IPS[2],443),(IPS[0],80)):
        try:r,w=await asyncio.wait_for(asyncio.open_connection(ip,port),.35)
        except (OSError,TimeoutError):pass
        else:w.close();raise AssertionError('Unexpected unapproved connection')
    with tempfile.TemporaryDirectory(prefix='gate-') as d:
        channels={k:Path(d)/str(i) for i,k in enumerate(('a'*24,'telegram'))}
        for x in channels.values():x.mkdir(mode=0o700)
        b=PinnedBroker(p,channels);stop=asyncio.Event();task=asyncio.create_task(b.serve(stop));writers=[];readers=[]
        try:
            await asyncio.wait_for(b.ready.wait(),2)
            for role,host in (('a'*24,'api.oneme.ru'),('telegram','api.telegram.org')):
                r,w,head=await request(channels[role],host);writers.append(w);readers.append(r);assert b'200' in head
                await w.start_tls(ctx,server_hostname=host,ssl_handshake_timeout=2);w.write(b'confirmed');await w.drain();assert await asyncio.wait_for(r.readexactly(9),2)==b'confirmed'
            for role,host in (('a'*24,'api.telegram.org'),('telegram','api.oneme.ru')):
                _,w,head=await request(channels[role],host);writers.append(w);assert b'403' in head
            assert await asyncio.wait_for(task,23)=='EXPIRED'
            for r in readers:
                try:assert await asyncio.wait_for(r.read(),2)==b''
                except (ConnectionResetError,ssl.SSLEOFError):pass
            assert not b.ready.is_set() and not any((x/'proxy.sock').exists() for x in channels.values())
            print('NAMESPACE TLS PASS: two actual routed TLS connections, foreign roles denied, expiry closed both',flush=True)
        finally:
            stop.set();await asyncio.gather(task,return_exceptions=True)
            for w in writers:
                w.close()
                try:await asyncio.wait_for(w.wait_closed(),.5)
                except (OSError,TimeoutError):pass

if __name__=='__main__':
    if sys.argv[1]=='server':asyncio.run(server())
    elif sys.argv[1]=='prepare':prepare(*sys.argv[2:])
    elif sys.argv[1]=='exercise':asyncio.run(exercise(sys.argv[2]))
    else:raise ValueError('Unknown fixture command')
