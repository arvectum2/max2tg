"""Single-use explicit recovery preparation, not a production launcher.

Caller must own both namespaces exclusively, establish stopped writers,
retire old connectivity, retain independent journal storage, and keep new
runtime stopped during preparation. No automatic retries or journal mutations.
"""
import hashlib
import json

from app.egress_policy import EgressDenied
from app.encrypted_storage import _read, _write
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import prepare_fresh_policy, verify_ruleset
from app.linux_pin_rotation import _handles
from app.rotation_fence import LIMIT, RotationFence, _binding, _bundle


def _require(ok):
    if not ok:raise EgressDenied('Recovery preparation denied')


class RecoveryTransition:
    """Persist a new intent beside the immutable old rotation fence.

    One explicit reviewed replacement, one preparation attempt. Reopening
    permits evidence inspection only, not launch or automatic adoption.
    """
    def __init__(self,old_fence,new_namespace):
        _require(type(old_fence) is RotationFence)
        self.old=old_fence
        self.new=new_namespace
        self.path=self.old.root/'recovery.json'
        self.new_identity=_binding(self.new)
        _require(self.new_identity['boot']==self.old.identity['boot']
                 and self.new_identity['namespace']!=self.old.identity['namespace'])
        self.used=False

    def _source(self):
        self.old._held()
        _require(self.old.grant is None)
        record=self.old._load()
        _require(_binding(self.old.namespace)==self.old.identity)
        raw=_read(self.old.path,LIMIT)
        _require(json.loads(raw)==record)
        return record,hashlib.sha256(raw).hexdigest()

    def _check(self,stopped):
        _require(stopped() is True)
        _require(self.old.namespace.retired() is True)
        _require(_binding(self.new)==self.new_identity)

    def _publish(self,record,*,replace):
        self.old._held()
        raw=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
        _require(len(raw)<=LIMIT)
        _write(self.path,raw,replace=replace)

    def prepare(self,pins,*,operator_approved,stopped,install):
        _require(not self.used)
        self.used=True
        _require(operator_approved is True and type(pins) is BrokerPins)
        _require(not self.path.exists() and not self.path.is_symlink())
        source,source_hash=self._source()
        replacement=_bundle(source['replacement'])
        pins=pins.validated()
        _require(pins.max_text==replacement.max_text and pins.telegram==replacement.telegram
                 and pins.expires_at>replacement.expires_at)
        self._check(stopped)
        record={'version':1,'state':'PREPARING','source_sha256':source_hash,
                'old_binding':self.old.identity,'new_binding':self.new_identity,
                'pins':pins.record(),'handles':[]}
        self._publish(record,replace=False)
        def observe():
            self._check(stopped)
            return self.new.observe()
        def apply(batch):
            self._check(stopped)
            install(batch)
        result=prepare_fresh_policy(pins,observe=observe,install=apply)
        observed=observe()
        verify_ruleset(pins,observed)
        _require(self._source()[1]==source_hash)
        self._check(stopped)
        self._publish(dict(record,state='PREPARED',handles=[list(h) for h in _handles(observed)]),replace=True)
        return dict(result,recovery_prepared=True,automatic_retry=False)

    def inspect(self):
        source,source_hash=self._source()
        v=json.loads(_read(self.path,LIMIT))
        _require(type(v) is dict and set(v)=={'version','state','source_sha256','old_binding','new_binding','pins','handles'}
                 and type(v['version']) is int and v['version']==1
                 and v['state'] in ('PREPARING','PREPARED') and v['source_sha256']==source_hash
                 and v['old_binding']==self.old.identity and v['new_binding']==self.new_identity)
        _require(self.old.namespace.retired() is True and _binding(self.new)==self.new_identity)
        pins=_bundle(v['pins'])
        replacement=_bundle(source['replacement'])
        _require(pins.max_text==replacement.max_text and pins.telegram==replacement.telegram
                 and pins.expires_at>replacement.expires_at)
        _require(type(v['handles']) is list and
                 (v['state']!='PREPARING' or v['handles']==[]) and
                 all(type(h) is list and len(h)==3 and type(h[0]) is str
                     and type(h[1]) is str and type(h[2]) is int and h[2]>0
                     for h in v['handles']))
        matches=False
        try:
            observed=self.new.observe()
            verify_ruleset(pins,observed)
            matches=(v['state']=='PREPARED' and [list(h) for h in _handles(observed)]==v['handles'])
        except EgressDenied:
            pass
        _require(self._source()[1]==source_hash and self.old.namespace.retired() is True
                 and _binding(self.new)==self.new_identity)
        return {'state':v['state'],'prepared_policy_matches':matches,'automatic_retry':False,
                'live_launch_authorized':False}
