"""Atomic nftables transaction acceptance in a disposable network-none container."""
import json
import os
from pathlib import Path
import subprocess
import time

from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import prepare_fresh_policy, verify_ruleset
from app.linux_pin_rotation import rotate_stopped_policy, rotation_batch
from tests.linux_egress_kernel_fixture import call, connect, counters


def main():
    assert os.getuid() == 0 and Path('/.dockerenv').is_file()
    assert not Path('/var/run/docker.sock').exists()
    status = Path('/proc/self/status').read_text()
    assert 'CapEff:\t0000000000001000' in status and 'NoNewPrivs:\t1' in status
    assert {v['ifname'] for v in json.loads(call('ip', '-j', 'link', 'show'))
            if 'UP' in v['flags']} == {'lo'}
    for fam in ('-4', '-6'):
        assert not json.loads(call('ip', '-j', fam, 'route', 'show', 'default'))
    for ip in ('8.8.8.8', '9.9.9.9', '1.1.1.1'):
        call('ip', 'address', 'add', ip + '/32', 'dev', 'lo')
    expiry = int(time.time()) + 10
    old = BrokerPins.parse({'max-text': ['8.8.8.8'], 'telegram': ['9.9.9.9'],
                            'expires_at': expiry})
    new = BrokerPins.parse({'max-text': ['1.1.1.1'], 'telegram': ['9.9.9.9'],
                            'expires_at': expiry + 6})

    def observe():
        return json.loads(call('nft', '-j', 'list', 'ruleset'))

    def apply(batch):
        call('nft', '-f', '-', stdin=batch)

    assert prepare_fresh_policy(old, observe=observe, install=apply)['verified']
    # A deliberately invalid second operation must roll back the preceding flush.
    bad = rotation_batch(old, new, now=time.time()).splitlines()[0] + '\n'
    bad += 'add element inet m2tg_egress nonexistent_set { 1.1.1.1 }\n'
    try:
        apply(bad)
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError('Invalid transaction succeeded')
    assert verify_ruleset(old, observe())['verified']
    print('PIN ROTATION ATOMIC ABORT PASS', flush=True)
    def handles():
        return [(kind, obj['handle']) for entry in observe()['nftables']
                for kind, obj in entry.items() if kind in ('table', 'chain', 'rule', 'set')]

    prior = handles()
    result = rotate_stopped_policy(old, new, observe=observe, apply=apply)
    assert result['rotated'] and not result['live_launch_authorized']
    assert handles() == prior
    before = counters('output')
    connect('8.8.8.8', 443)
    after = counters('output')
    assert after[0] == before[0] and after[1] > before[1]
    for ip in ('1.1.1.1', '9.9.9.9'):
        connect(ip, 443)
    final = counters('output')
    assert final[0] >= after[0] + 2 and final[1] == after[1]
    print('PIN ROTATION KERNEL PASS: old pin blocked, new and retained pins allowed, rule handles unchanged', flush=True)


if __name__ == '__main__':
    main()
