"""Routed TLS and nftables test in a disposable network-none container."""
import asyncio
import json
import os
from pathlib import Path
import ssl
import select
import subprocess
import sys
import tempfile
import time

from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import prepare_fresh_policy
from app.pinned_broker import PinnedBroker
from tests.pinned_broker_linux_fixture import request

IPS = ('8.8.8.8', '9.9.9.9', '1.1.1.1')  # synthetic local aliases only
DROP = ['setpriv', '--reuid=65532', '--regid=65532', '--clear-groups',
        '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all']


def call(*args, data=None):
    return subprocess.run(args, input=data, text=True, capture_output=True,
                          check=True, timeout=5).stdout


def isolated(prefix, interfaces):
    assert Path('/.dockerenv').exists()
    assert not Path('/var/run/docker.sock').exists()
    assert {i['ifname'] for i in json.loads(call(*prefix, 'ip', '-j', 'link'))
            if 'UP' in i['flags']} == interfaces
    for family in ('-4', '-6'):
        assert not json.loads(call(*prefix, 'ip', '-j', family, 'route', 'show', 'default'))


def unprivileged():
    status = Path('/proc/self/status').read_text()
    assert os.getuid() == os.getgid() == 65532
    assert 'NoNewPrivs:\t1' in status
    assert all(f'{key}:\t0000000000000000' in status
               for key in ('CapEff', 'CapPrm', 'CapBnd', 'CapInh', 'CapAmb'))


async def server(cert):
    unprivileged()
    isolated([], {'lo', 'm2server'})
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert + '/cert.pem', cert + '/key.pem')

    async def echo(reader, writer):
        try:
            while data := await reader.read(128):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    listeners = [await asyncio.start_server(echo, ip, 443, ssl=ctx) for ip in IPS]
    listeners.append(await asyncio.start_server(echo, IPS[0], 80))
    print('MOCK READY', flush=True)
    try:
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
    finally:
        for listener in listeners:
            listener.close()
            await listener.wait_closed()


async def denied(ip, port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), .4)
    except (OSError, TimeoutError):
        return
    writer.close()
    raise AssertionError('Unexpected allowed connection')


async def exercise(cert, expiry):
    unprivileged()
    isolated([], {'lo', 'eth0'})
    for ip in IPS:
        route, = json.loads(call('ip', '-j', 'route', 'get', ip))
        assert route['dev'] == 'eth0' and route['gateway'] == '192.0.2.1'
    await denied(IPS[2], 443)
    await denied(IPS[0], 80)
    ctx = ssl.create_default_context(cafile=cert + '/cert.pem')
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    pins = BrokerPins.parse({'max-text': [IPS[0]], 'telegram': [IPS[1]],
                             'expires_at': expiry})
    with tempfile.TemporaryDirectory(prefix='veth-broker-') as d:
        channels = {k: Path(d) / str(i) for i, k in enumerate(('a'*24, 'telegram'))}
        for path in channels.values():
            path.mkdir(mode=0o700)
        broker = PinnedBroker(pins, channels)
        stop = asyncio.Event()
        task = asyncio.create_task(broker.serve(stop))
        readers, writers = [], []
        try:
            await asyncio.wait_for(broker.ready.wait(), 2)
            for role, host in (('a'*24, 'api.telegram.org'), ('telegram', 'api.oneme.ru')):
                _, writer, head = await request(channels[role], host)
                writers.append(writer)
                assert b'403' in head
            for role, host in (('a'*24, 'api.oneme.ru'), ('telegram', 'api.telegram.org')):
                reader, writer, head = await request(channels[role], host)
                readers.append(reader)
                writers.append(writer)
                assert b'200' in head
                await writer.start_tls(ctx, server_hostname=host, ssl_handshake_timeout=2)
                writer.write(b'confirmed')
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(9), 2) == b'confirmed'
            assert await asyncio.wait_for(task, 20) == 'EXPIRED'
            for reader in readers:
                try:
                    assert await asyncio.wait_for(reader.read(), 2) == b''
                except (ConnectionResetError, ssl.SSLEOFError):
                    pass
            assert not broker.ready.is_set()
            assert not any((p / 'proxy.sock').exists() for p in channels.values())
            print('VETH TLS PASS: 2 allowed, 2 foreign roles denied, 2 tunnels closed', flush=True)
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            for writer in writers:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), .5)
                except (OSError, TimeoutError):
                    pass
    # Broker stops conservatively early. Test kernel after absolute deadline.
    await asyncio.sleep(max(0, expiry - time.time()) + .2)
    for ip in IPS[:2]:
        await denied(ip, 443)
    print('VETH KERNEL EXPIRED PINS DENIED', flush=True)



def main(rotation=False, clients=False):
    isolated([], {'lo'})
    assert os.getuid() == 0 and 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    assert not json.loads(call('ip', '-j', '-4', 'route', 'show', 'table', 'main'))
    keeper = subprocess.Popen(['unshare', '--net', 'sleep', '50'])
    mock = None
    client_root = None
    try:
        for _ in range(50):
            if os.readlink('/proc/self/ns/net') != os.readlink(f'/proc/{keeper.pid}/ns/net'):
                break
            time.sleep(.05)
        else:
            raise AssertionError('New namespace not created')
        ns = ['nsenter', '-t', str(keeper.pid), '-n', '--']
        call('ip', 'link', 'add', 'm2server', 'type', 'veth', 'peer', 'name', 'm2broker')
        call('ip', 'link', 'set', 'm2broker', 'netns', str(keeper.pid))
        call('ip', 'addr', 'add', '192.0.2.1/30', 'dev', 'm2server')
        call('ip', 'link', 'set', 'm2server', 'up')
        call(*ns, 'ip', 'link', 'set', 'm2broker', 'name', 'eth0')
        call(*ns, 'ip', 'addr', 'add', '192.0.2.2/30', 'dev', 'eth0')
        call(*ns, 'ip', 'link', 'set', 'eth0', 'up')
        call(*ns, 'ip', 'link', 'set', 'lo', 'up')
        for ip in IPS:
            call('ip', 'addr', 'add', ip+'/32', 'dev', 'lo')
            call(*ns, 'ip', 'route', 'add', ip+'/32', 'via', '192.0.2.1', 'dev', 'eth0')
        isolated([], {'lo', 'm2server'})
        isolated(ns, {'lo', 'eth0'})
        with tempfile.TemporaryDirectory(prefix='veth-cert-') as d:
            os.chmod(d, 0o755)
            call('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                 '-days', '1', '-subj', '/CN=synthetic',
                 '-addext', 'subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org',
                 '-keyout', d+'/key.pem', '-out', d+'/cert.pem')
            os.chmod(d+'/key.pem', 0o644)  # ephemeral synthetic key only
            if clients:
                client_root=call(*DROP,sys.executable,'-c',
                    'import tempfile;from pathlib import Path;p=Path(tempfile.mkdtemp(prefix="client-rotation-"));[(p/n).mkdir(mode=0o700) for n in ("max","tg")];print(p)').strip()
                args=['tests.linux_client_rotation_fixture','server',d,client_root]
            else:
                args=['tests.linux_veth_fixture','server',d]
            mock = subprocess.Popen([*DROP, sys.executable, '-m', *args],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert select.select([mock.stdout], [], [], 5)[0], 'Mock readiness timeout'
            assert mock.stdout.readline().strip() == 'MOCK READY'
            expiry = int(time.time()) + (30 if clients else 14)
            pins = BrokerPins.parse({'max-text': [IPS[0]], 'telegram': [IPS[1]],
                                     'expires_at': expiry})
            result = prepare_fresh_policy(
                pins, observe=lambda: json.loads(call(*ns, 'nft', '-j', 'list', 'ruleset')),
                install=lambda rules: call(*ns, 'nft', '-f', '-', data=rules))
            assert result['verified'] and not result['live_launch_authorized']
            print('VETH EXACT KERNEL GATE PASS', flush=True)
            if clients:
                from tests.linux_client_rotation_fixture import controller
                controller(ns,d,pins,client_root)
            elif rotation:
                from tests.linux_live_rotation_fixture import controller
                controller(ns, d, pins)
            else:
                subprocess.run([*ns, *DROP, sys.executable, '-m', 'tests.linux_veth_fixture',
                                'exercise', d, str(expiry)], check=True, timeout=25)
            counts = [e['counter']['packets'] for i in json.loads(call(
                *ns, 'nft', '-j', 'list', 'chain', 'inet', 'm2tg_egress', 'output'))['nftables']
                      if 'rule' in i for e in i['rule']['expr'] if 'counter' in e]
            assert counts[0] > 2 and counts[1] >= (1 if rotation or clients else 4), counts
            print('VETH KERNEL COUNTERS PASS', counts, flush=True)
    finally:
        try:
            if mock is not None:
                mock.stdin.close()
                assert mock.wait(timeout=5) == 0
                out=mock.stdout.read()
                if clients:
                    assert 'CLIENT EFFECTS PASS' in out
                    print(out,flush=True)
                mock.stdout.close()
        finally:
            try:
                if client_root is not None:
                    call(*DROP,sys.executable,'-c','import shutil,sys;shutil.rmtree(sys.argv[1])',client_root)
            finally:
                keeper.terminate()
                keeper.wait(timeout=3)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        main()
    elif sys.argv[1] == '--clients':
        main(clients=True)
    elif sys.argv[1] == '--rotation':
        main(rotation=True)
    elif sys.argv[1] == 'server':
        asyncio.run(server(sys.argv[2]))
    elif sys.argv[1] == 'exercise':
        asyncio.run(exercise(sys.argv[2], int(sys.argv[3])))
    else:
        raise ValueError('Unknown fixture action')
