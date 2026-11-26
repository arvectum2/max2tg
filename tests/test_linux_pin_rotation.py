from copy import deepcopy

import pytest

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.linux_pin_rotation import rotate_stopped_policy, rotation_batch
from tests.test_linux_namespace_gate import NOW, PINS, sample


def replacement(**patch):
    return BrokerPins.parse({'max-text': ['1.1.1.1'], 'telegram': ['9.9.9.9'],
                             'expires_at': 1120} | patch, now=NOW)


def observed_new():
    result = sample()
    result['nftables'][5]['set']['elem'] = [
        {'elem': {'val': ip, 'timeout': 120, 'expires': 115}}
        for ip in ('1.1.1.1', '9.9.9.9')]
    return result


def test_atomic_exact_set_batch():
    assert rotation_batch(PINS, replacement(), now=NOW) == (
        'flush set inet m2tg_egress api_ipv4\n'
        'add element inet m2tg_egress api_ipv4 { 1.1.1.1 timeout 120s, 9.9.9.9 timeout 120s }\n')


@pytest.mark.parametrize('patch', [
    {'expires_at': 1060}, {'expires_at': 1059},
    {'max-text': ['9.9.9.9'], 'telegram': ['1.1.1.1']},
    {'telegram': ['8.8.8.8']},
])
def test_reject_invalid_generation(patch):
    with pytest.raises(EgressDenied):
        rotation_batch(PINS, replacement(**patch), now=NOW)


def test_renew_same_addresses():
    new = replacement(**{'max-text': ['8.8.8.8']})
    assert '8.8.8.8 timeout 120s' in rotation_batch(PINS, new, now=NOW)


def test_valid_rotation_once_and_no_launch():
    before, after = sample(), observed_new()
    expected = deepcopy([before, after])
    reads = iter([before, after])
    calls = []
    result = rotate_stopped_policy(PINS, replacement(), observe=lambda: next(reads),
                                   apply=calls.append, clock=lambda: NOW)
    assert result == {'verified': True, 'pins': 2, 'rotated': True,
                      'live_launch_authorized': False}
    assert calls == [rotation_batch(PINS, replacement(), now=NOW)]
    assert [before, after] == expected


def test_preexisting_drift_prevents_mutation():
    before = sample()
    before['nftables'].append(deepcopy(before['nftables'][-1]))
    calls = []
    with pytest.raises(EgressDenied):
        rotate_stopped_policy(PINS, replacement(), observe=lambda: before,
                              apply=calls.append, clock=lambda: NOW)
    assert not calls


def test_ambiguous_apply_never_retries():
    calls = []
    def fail(batch):
        calls.append(batch)
        raise TimeoutError('ambiguous kernel mutation')
    with pytest.raises(TimeoutError):
        rotate_stopped_policy(PINS, replacement(), observe=sample,
                              apply=fail, clock=lambda: NOW)
    assert len(calls) == 1


def test_postcondition_failure_never_retries_or_rolls_back():
    calls = []
    with pytest.raises(EgressDenied):
        rotate_stopped_policy(PINS, replacement(), observe=sample,
                              apply=calls.append, clock=lambda: NOW)
    assert len(calls) == 1


def test_expiry_during_read_prevents_mutation():
    clock = iter([NOW, 1060])
    calls = []
    with pytest.raises(EgressDenied):
        rotate_stopped_policy(PINS, replacement(), observe=sample,
                              apply=calls.append, clock=lambda: next(clock))
    assert not calls


def test_changed_rule_handle_rejected_without_retry():
    after = observed_new()
    after['nftables'][6]['rule']['handle'] = 999
    observations = iter([sample(), after])
    calls = []
    with pytest.raises(EgressDenied, match='identity changed'):
        rotate_stopped_policy(PINS, replacement(), observe=lambda: next(observations),
                              apply=calls.append, clock=lambda: NOW)
    assert len(calls) == 1
