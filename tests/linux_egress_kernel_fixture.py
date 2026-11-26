"""Actual nftables packet counters in a disposable network-none container only.
Public-looking synthetic addresses are assigned to loopback, never routed outside.
"""
import json,os,socket,subprocess,time
from pathlib import Path
from app.linux_egress_policy import BrokerPins


def call(*args,stdin=None):
    r=subprocess.run(args,input=stdin,text=True,capture_output=True,timeout=10,check=True);return r.stdout


def counters(chain):
    data=json.loads(call('nft','-j','list','chain','inet','m2tg_egress',chain))
    return [e['counter']['packets'] for item in data['nftables'] if 'rule' in item for e in item['rule']['expr'] if 'counter' in e]


def connect(address,port,family=socket.AF_INET):
    with socket.socket(family,socket.SOCK_STREAM) as s:
        s.settimeout(.3)
        try:s.connect((address,port))
        except OSError:return
        raise AssertionError('Unexpected connection in isolated no-listener fixture')


def main():
    assert os.getuid()==0 and Path('/.dockerenv').is_file() and not Path('/var/run/docker.sock').exists()
    status=Path('/proc/self/status').read_text();assert 'CapEff:\t0000000000001000' in status and 'NoNewPrivs:\t1' in status
    assert {v['ifname'] for v in json.loads(call('ip','-j','link','show')) if 'UP' in v['flags']}=={'lo'}
    assert len(Path('/proc/net/route').read_text().splitlines())==1
    assert not json.loads(call('nft','-j','list','ruleset'))['nftables'][1:]
    call('ip','link','set','lo','up')
    for ip in ('8.8.8.8','9.9.9.9','1.1.1.1'):call('ip','address','add',ip+'/32','dev','lo')
    assert {v['ifname'] for v in json.loads(call('ip','-j','link','show')) if 'UP' in v['flags']}=={'lo'}
    pins=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':int(time.time())+12})
    rules=pins.render();call('nft','-c','-f','-',stdin=rules);call('nft','-f','-',stdin=rules)
    initial=counters('output');assert initial==[0,0]
    for ip in ('8.8.8.8','9.9.9.9'):connect(ip,443)
    allowed=counters('output');assert allowed[0]>=2 and allowed[1]==0
    assert counters('input')[1]>=2 # incoming NEW denied
    connect('1.1.1.1',443);connect('8.8.8.8',80);connect('127.0.0.1',443)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
        try:s.sendto(b'synthetic',('8.8.8.8',53))
        except PermissionError:pass
    denied=counters('output');assert denied[0]==allowed[0] and denied[1]>=4
    deadline=time.monotonic()+15
    while True:
        items=json.loads(call('nft','-j','list','set','inet','m2tg_egress','api_ipv4'))['nftables'];sets=[x['set'] for x in items if 'set' in x]
        if len(sets)==1 and not sets[0].get('elem'):break
        if time.monotonic()>deadline:raise AssertionError('Kernel pins did not expire')
        time.sleep(.25)
    prior=counters('output');connect('8.8.8.8',443);expired=counters('output');assert expired[0]==prior[0] and expired[1]>prior[1]
    print('LINUX EGRESS KERNEL PASS '+json.dumps({'allowed_output_packets':allowed[0],'denied_output_packets':denied[1],'incoming_new_denied':True,'expired_pin_denied':True,'external_interfaces':0},sort_keys=True))

if __name__=='__main__':main()
