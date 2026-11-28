"""Bounded sequential pin rotations within one trusted controller lifetime.

Retain independently of runtime state. No adoption on restart, no launch
authority, no namespace creation. Caller owns namespace and broker exclusively.
"""
import hashlib
import json
import re
import secrets
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
        base={'version','binding','initial','handles','rotations'}
        require(type(value) is dict and type(value.get('version')) is int and
                ((value['version']==1 and set(value)==base) or
                 (value['version']==2 and set(value)==base|{'predecessor','activation'}))
                and value['binding']==self.binding and type(value['rotations']) is list
                and len(value['rotations'])<=MAX_ROTATIONS)
        require(type(value['handles']) is list and len(value['handles'])==10 and
                all(type(h) is list and len(h)==3 and type(h[0]) is str
                    and type(h[1]) is str and type(h[2]) is int and h[2]>0 for h in value['handles']))
        history=[]
        nonces=set()
        if value['version']==2:
            prior=value['predecessor'];activation=value['activation']
            require(type(prior) is dict and set(prior)=={'source_sha256','history'}
                    and type(prior['source_sha256']) is str
                    and re.fullmatch('[0-9a-f]{64}',prior['source_sha256'])
                    and type(prior['history']) is list and 1<=len(prior['history'])<=MAX_ROTATIONS)
            require(self.root.name=='successor')
            parent_raw=_read(self.root.parent/'generations.json',LIMIT)
            require(hashlib.sha256(parent_raw).hexdigest()==prior['source_sha256'])
            parent=json.loads(parent_raw)
            expected=parent.get('predecessor',{}).get('history',[])+[parent['initial']]+[
                entry['pins'] for entry in parent['rotations']]
            require(expected==prior['history'])
            transition=json.loads(_read(self.root.parent/'recovery.json',16384),object_pairs_hook=unique)
            require(type(transition) is dict and
                    set(transition)=={'version','state','source_sha256','old_binding','new_binding','pins','handles'}
                    and type(transition['version']) is int and transition['version']==1
                    and transition['state']=='PREPARED'
                    and transition['source_sha256']==prior['source_sha256']
                    and transition['old_binding']==parent['binding']
                    and transition['new_binding']==self.binding
                    and transition['pins']==value['initial']
                    and transition['handles']==value['handles'])

            for record in prior['history']:
                pins=_bundle(record)
                require(not history or pins.expires_at>history[-1].expires_at)
                check_roles(history,pins);history.append(pins)
            require(type(activation) is dict and set(activation)=={'nonce','state'}
                    and type(activation['nonce']) is str and re.fullmatch('[0-9a-f]{48}',activation['nonce'])
                    and activation['state'] in ('PREPARED','ADOPTED')
                    and (not value['rotations'] or activation['state']=='ADOPTED'))
            nonces.add(activation['nonce'])
        initial=_bundle(value['initial'])
        if history:
            last=history[-1]
            require(initial.max_text==last.max_text and initial.telegram==last.telegram
                    and initial.expires_at>last.expires_at)
        history.append(initial)
        require(len(history)-1+len(value['rotations'])<=MAX_ROTATIONS)
        last_index=len(history)-1+len(value['rotations'])
        for index,r in enumerate(value['rotations'],len(history)):
            require(type(r) is dict and set(r)=={'generation','nonce','state','pins'}
                    and type(r['generation']) is int and r['generation']==index
                    and type(r['nonce']) is str and len(r['nonce'])==48
                    and all(c in '0123456789abcdef' for c in r['nonce'])
                    and r['nonce'] not in nonces
                    and r['state'] in ('ARMED','IN_FLIGHT','KERNEL_VERIFIED','ADOPTED')
                    and (index==last_index or r['state']=='ADOPTED'))
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
                self.root.name!='successor' and self.record is None and not self.path.exists() and not self.path.is_symlink())
        require({p.name for p in self.root.iterdir()}=={'generations.lock'})
        handles=self._verify(pins)
        self._save({'version':1,'binding':self.binding,'initial':pins.record(),
                    'handles':handles,'rotations':[]},replace=False)
        self.usable=True

    def _initialize_recovered(self,pins,history,source_sha256):
        require(self.root.name=='successor' and self.record is None and
                not self.path.exists() and not self.path.is_symlink())
        require(hashlib.sha256(_read(self.root.parent/'generations.json',LIMIT)).hexdigest()==source_sha256)
        require({p.name for p in self.root.iterdir()}=={'generations.lock'})
        require(1<=len(history)<=MAX_ROTATIONS and pins.expires_at>history[-1].expires_at
                and pins.max_text==history[-1].max_text and pins.telegram==history[-1].telegram)
        check_roles(history,pins)
        handles=self._verify(pins)
        nonce=secrets.token_hex(24)
        self._save({'version':2,'binding':self.binding,'initial':pins.record(),
                    'handles':handles,'rotations':[],
                    'predecessor':{'source_sha256':source_sha256,'history':[p.record() for p in history]},
                    'activation':{'nonce':nonce,'state':'PREPARED'}},replace=False)
        return nonce

    def confirm_recovered(self,*,nonce,pin_digest,broker_ready):
        require(not self.usable and self.grant is None and self.record is not None)
        value,history=self._current()
        require(value['version']==2 and not value['rotations'] and value['activation']['state']=='PREPARED')
        self.record=None
        require(broker_ready is True and nonce==value['activation']['nonce'] and pin_digest==digest(history[-1]))
        self._verify(history[-1],value['handles'])
        self._save(dict(value,activation=dict(value['activation'],state='ADOPTED')),replace=True)
        self.usable=True

    def arm(self,current,replacement):
        require(self.usable and self.grant is None)
        value,history=self._current()
        entries=value['rotations']
        require(len(history)-1<MAX_ROTATIONS and (not entries or entries[-1]['state']=='ADOPTED')
                and current==history[-1])
        rotation_batch(current,replacement,now=time.time())
        check_roles(history,replacement)
        self._verify(current,value['handles'])
        grant=RotationGrant(self.namespace,current,replacement)
        entry={'generation':len(history),'nonce':grant.nonce,
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
        state=entries[-1]['state'] if entries else value.get('activation',{}).get('state','INITIAL')
        matches=[]
        offset=len(history)-len(entries)-1
        for i in range(max(offset,len(history)-2),len(history)):
            try:
                self._verify(history[i],value['handles'])
                matches.append(i)
            except EgressDenied:pass
        return {'generation':len(history)-1,'state':state,'matching_generations':matches,
                'automatic_retry':False,'live_launch_authorized':False}

    def close(self):
        self.usable=False;self.grant=None;self.record=None;self.lock.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
