"""Trusted, offline kernel pin rotation contract.

Caller must hold exclusive namespace ownership and keep the broker stopped.
Callbacks are trusted; an ambiguous mutation must be investigated, not retried.
No namespace selection, DNS resolution, privileges or live launch authorization.
"""
import time

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import verify_ruleset


def rotation_batch(current, replacement, *, now):
    if type(current) is not BrokerPins or type(replacement) is not BrokerPins:
        raise EgressDenied('Exact reviewed pin generations required')
    current = current.validated(now=now)
    replacement = replacement.validated(now=now)
    if replacement.expires_at <= current.expires_at:
        raise EgressDenied('Replacement must advance the expiry')
    if (set(current.max_text) & set(replacement.telegram)
            or set(current.telegram) & set(replacement.max_text)):
        raise EgressDenied('Cross-role pin transfer prohibited')
    # One nft transaction, not two commands. Only the verified owned set changes;
    # chains remain default-drop and all existing rule handles remain untouched.
    ttl = replacement.remaining(now=now)
    elements = ', '.join(f'{ip} timeout {ttl}s'
                         for ip in replacement.max_text + replacement.telegram)
    return ('flush set inet m2tg_egress api_ipv4\n'
            f'add element inet m2tg_egress api_ipv4 {{ {elements} }}\n')


def _handles(observed):
    return sorted((kind, obj.get('chain', obj.get('name', '')), obj['handle'])
                  for entry in observed['nftables']
                  for kind, obj in entry.items() if kind != 'metainfo')


def rotate_stopped_policy(current, replacement, *, observe, apply, clock=time.time):
    """Verify old policy, apply once, verify new policy. Never retry/rollback."""
    now = clock()
    rotation_batch(current, replacement, now=now)
    before = observe()
    verify_ruleset(current, before, now=clock())
    handles = _handles(before)
    batch = rotation_batch(current, replacement, now=clock())
    apply(batch)
    after = observe()
    result = verify_ruleset(replacement, after, now=clock())
    if _handles(after) != handles:
        raise EgressDenied('Kernel object identity changed during rotation')
    return dict(result, rotated=True)
