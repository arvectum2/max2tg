"""Persistent single-grant fence. Reopening is observation only, never re-arming.

Trusted operator retains this directory independently of runtime/restored state.
No recovery can authorize launch or adopt kernel state automatically.
"""
import json
import re

from app.egress_policy import EgressDenied
from app.encrypted_storage import _read, _write
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import verify_ruleset
from app.linux_pin_rotation import _handles, rotation_batch
from app.operator_pin_rotation import RotationGrant
from app.recovery_manifest import _private_dir
from app.runtime_lock import RuntimeLock

LIMIT=16384


def _deny(message):
    raise EgressDenied(message)


def _binding(namespace):
    namespace.check()
    return namespace.binding()


def _bundle(record):
    # Expired bundles can be inspected structurally, not used for admission.
    if type(record) is not dict or type(record.get('expires_at')) is not int:
        _deny('Invalid retained generation')
    return BrokerPins.parse(record,now=record['expires_at']-1)


class RotationFence:
    """One namespace-bound grant with persistent intent before mutation.

    ARMED -> IN_FLIGHT -> KERNEL_VERIFIED. All retained states block new grants.
    A retained record may be inspected after restart, never executed again.
    """
    def __init__(self,folder,namespace):
        self.root=_private_dir(folder)
        self.path=self.root/'rotation.json'
        self.namespace=namespace
        self.lock=RuntimeLock(self.root/'rotation.lock')
        self.grant=None
        self.record=None
        try:
            self.identity=_binding(namespace)
        except BaseException:
            self.close()
            raise

    def _held(self):
        if self.lock.fd is None:_deny("Rotation fence closed")

    def _publish(self,record,*,replace):
        self._held()
        data=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
        if len(data)>LIMIT:_deny('Rotation fence size limit')
        _write(self.path,data,replace=replace)
        self.record=record

    def _load(self):
        self._held()
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:_deny('Duplicate fence field')
                result[key]=value
            return result
        value=json.loads(_read(self.path,LIMIT),object_pairs_hook=unique)
        if (type(value) is not dict or set(value)!={'version','binding','state','nonce','current','replacement','handles'}
                or type(value['version']) is not int or value['version']!=1
                or value['binding']!=self.identity
                or value['state'] not in ('ARMED','IN_FLIGHT','KERNEL_VERIFIED')
                or type(value['nonce']) is not str or not re.fullmatch('[0-9a-f]{48}',value['nonce'])
                or type(value['handles']) is not list
                or any(type(h) is not list or len(h)!=3 or type(h[0]) is not str or type(h[1]) is not str
                       or type(h[2]) is not int or h[2]<=0 for h in value['handles'])):
            _deny('Invalid or foreign rotation fence')
        old,new=_bundle(value['current']),_bundle(value['replacement'])
        # Validate the historical transition at a time both bundles were valid.
        rotation_batch(old,new,now=old.expires_at-1)
        return value

    def arm(self,current,replacement):
        self._held()
        if self.grant is not None or self.path.exists() or self.path.is_symlink():
            _deny('Retained generation requires explicit reconciliation')
        if _binding(self.namespace)!=self.identity:_deny('Namespace binding changed')
        grant=RotationGrant(self.namespace,current,replacement)
        observed=self.namespace.observe()
        verify_ruleset(current,observed)
        record={'version':1,'binding':self.identity,'state':'ARMED','nonce':grant.nonce,
                'current':current.record(),'replacement':replacement.record(),
                'handles':[list(h) for h in _handles(observed)]}
        self._publish(record,replace=False)
        self.grant=grant
        return grant.nonce

    def execute(self,raw):
        grant=self.grant
        # No re-arm or in-process retry even when writing the fence fails.
        self.grant=None
        if grant is None or self.record is None or self.record['state']!='ARMED':
            _deny('No fresh rotation grant')
        if self._load()!=self.record or _binding(self.namespace)!=self.identity:
            _deny('Rotation fence changed')
        observed=self.namespace.observe()
        verify_ruleset(grant.current,observed)
        if [list(h) for h in _handles(observed)]!=self.record['handles']:
            _deny('Kernel object identity changed')
        self._publish(dict(self.record,state='IN_FLIGHT'),replace=True)
        receipt=grant.execute(raw)
        # Kernel verified by grant, but delivery of receipt and broker adoption
        # are not atomic with this write. A crash here still requires review.
        self._publish(dict(self.record,state='KERNEL_VERIFIED'),replace=True)
        return receipt

    def inspect(self):
        """Read-only classification of retained evidence. Never clear or mutate."""
        value=self._load()
        if _binding(self.namespace)!=self.identity:_deny('Namespace binding changed')
        observed=self.namespace.observe()
        matched=[]
        for name in ('current','replacement'):
            try:
                verify_ruleset(_bundle(value[name]),observed)
                if [list(h) for h in _handles(observed)]==value['handles']:
                    matched.append(name)
            except EgressDenied:
                pass
        if _binding(self.namespace)!=self.identity:_deny('Namespace binding changed')
        return {'state':value['state'],'matches':matched,'automatic_retry':False,
                'live_launch_authorized':False,'requires_reconciliation':True}

    def close(self):
        self.grant=None
        self.lock.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
