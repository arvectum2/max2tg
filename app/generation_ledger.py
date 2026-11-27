"""Bounded sequential pin rotations within one trusted controller lifetime.

Retain independently of runtime state. No adoption on restart, no launch
authority, no namespace creation. Caller owns namespace and broker exclusively.
"""
import json
import time

from app.egress_policy import EgressDenied
from app.encrypted_storage import _read, _write
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import verify_ruleset
from app.linux_pin_rotation import _handles, rotation_batch
from app.operator_pin_rotation import RotationGrant, digest
from app.recovery_manifest import _private_dir
from app.rotation_fence import _binding, _bundle
from app.runtime_lock import RuntimeLock

MAX_ROTATIONS=64
LIMIT=256*1024


def require(ok):
    if not ok:raise EgressDenied('Generation ledger denied')


def check_roles(history, pins):
    for old in history:
        require(not (set(old.max_text)&set(pins.telegram)
                     or set(old.telegram)&set(pins.max_text)))


class GenerationLedger:
    def __init__(self,folder,namespace):
        self.root=_private_dir(folder)
        self.path=self.root/'generations.json'
        self.namespace=namespace
        self.lock=RuntimeLock(self.root/'generations.lock')
        self.record=None
        self.grant=None
        self.usable=False
        try:self.binding=_binding(namespace)
        except BaseException:
            self.close();raise

    def _held(self):
        require(self.lock.fd is not None and _binding(self.namespace)==self.binding)

    def _save(self,record,*,replace):
        self._held()
        raw=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
        require(len(raw)<=LIMIT)
        _write(self.path,raw,replace=replace)
        self.record=record

    def _load(self):
        self._held()
        def unique(pairs):
            result={}
            for k,v in pairs:
                require(k not in result)
                result[k]=v
            return result
        value=json.loads(_read(self.path,LIMIT),object_pairs_hook=unique)
        require(type(value) is dict and set(value)=={'version','binding','initial','handles','rotations'}
                and type(value['version']) is int and value['version']==1
                and value['binding']==self.binding and type(value['rotations']) is list
                and len(value['rotations'])<=MAX_ROTATIONS)
        require(type(value['handles']) is list and len(value['handles'])==10 and
                all(type(h) is list and len(h)==3 and type(h[0]) is str
                    and type(h[1]) is str and type(h[2]) is int and h[2]>0 for h in value['handles']))
        history=[_bundle(value['initial'])]
        nonces=set()
        for index,r in enumerate(value['rotations'],1):
            require(type(r) is dict and set(r)=={'generation','nonce','state','pins'}
                    and type(r['generation']) is int and r['generation']==index
                    and type(r['nonce']) is str and len(r['nonce'])==48
                    and all(c in '0123456789abcdef' for c in r['nonce'])
                    and r['nonce'] not in nonces
                    and r['state'] in ('ARMED','IN_FLIGHT','KERNEL_VERIFIED','ADOPTED')
                    and (index==len(value['rotations']) or r['state']=='ADOPTED'))
            nonces.add(r['nonce'])
            pins=_bundle(r['pins'])
            rotation_batch(history[-1],pins,now=history[-1].expires_at-1)
            check_roles(history,pins)
            history.append(pins)
        return value,history

    def _current(self):
        value,history=self._load()
        require(self.record==value)
        return value,history

    def _verify(self,pins,handles=None):
        self._held()
        observed=self.namespace.observe()
        verify_ruleset(pins,observed)
        actual=[list(h) for h in _handles(observed)]
        if handles is not None:require(actual==handles)
        self._held()
        return actual

    def initialize(self,pins,*,operator_approved):
        require(operator_approved is True and type(pins) is BrokerPins and
                self.record is None and not self.path.exists() and not self.path.is_symlink())
        require({p.name for p in self.root.iterdir()}=={'generations.lock'})
        handles=self._verify(pins)
        self._save({'version':1,'binding':self.binding,'initial':pins.record(),
                    'handles':handles,'rotations':[]},replace=False)
        self.usable=True

    def arm(self,current,replacement):
        require(self.usable and self.grant is None)
        value,history=self._current()
        entries=value['rotations']
        require(len(entries)<MAX_ROTATIONS and (not entries or entries[-1]['state']=='ADOPTED')
                and current==history[-1])
        rotation_batch(current,replacement,now=time.time())
        check_roles(history,replacement)
        self._verify(current,value['handles'])
        grant=RotationGrant(self.namespace,current,replacement)
        entry={'generation':len(entries)+1,'nonce':grant.nonce,
               'state':'ARMED','pins':replacement.record()}
        self.usable=False
        self._save(dict(value,rotations=entries+[entry]),replace=True)
        self.grant=grant
        return grant.nonce

    def _advance(self,value,state):
        entries=value['rotations']
        self._save(dict(value,rotations=entries[:-1]+[dict(entries[-1],state=state)]),replace=True)

    def execute(self,raw):
        grant=self.grant
        self.grant=None
        require(grant is not None)
        value,history=self._current()
        require(value['rotations'][-1]['state']=='ARMED')
        self._verify(history[-2],value['handles'])
        self._advance(value,'IN_FLIGHT')
        receipt=grant.execute(raw)
        self._verify(history[-1],value['handles'])
        self._advance(self.record,'KERNEL_VERIFIED')
        return receipt

    def confirm_adopted(self,*,nonce,pin_digest,broker_ready):
        # Trusted private broker acknowledgement, not independent attestation.
        # Any invalid or ambiguous confirmation permanently blocks this instance.
        require(not self.usable and self.grant is None and self.record is not None)
        value,history=self._current()
        require(value['rotations'] and value['rotations'][-1]['state']=='KERNEL_VERIFIED')
        self.record=None
        entry=value['rotations'][-1]
        require(broker_ready is True and nonce==entry['nonce'] and pin_digest==digest(history[-1]))
        self._verify(history[-1],value['handles'])
        self._advance(value,'ADOPTED')
        self.usable=True

    def inspect(self):
        value,history=self._load()
        entries=value['rotations']
        state=entries[-1]['state'] if entries else 'INITIAL'
        matches=[]
        for i in range(max(0,len(history)-2),len(history)):
            try:
                self._verify(history[i],value['handles'])
                matches.append(i)
            except EgressDenied:pass
        return {'generation':len(entries),'state':state,'matching_generations':matches,
                'automatic_retry':False,'live_launch_authorized':False}

    def close(self):
        self.usable=False;self.grant=None;self.record=None;self.lock.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
