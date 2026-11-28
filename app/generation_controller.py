"""Trusted framed-channel orchestration. No launch or privilege authority.

Caller supplies private, bounded transport and an independent stop-and-verify
callback. On ambiguity never retry: invalidate the local controller and require
the operator to reconcile. Stopping processes is the caller's responsibility.
"""
from app.egress_policy import EgressDenied
from app.controller_lease import renew
from app.generation_ledger import GenerationLedger, require
from app.operator_pin_rotation import frame, parse


class GenerationController:
    def __init__(self,ledger,*,send,receive,stop_and_verify):
        require(type(ledger) is GenerationLedger)
        self.ledger=ledger
        self.send=send
        self.receive=receive
        self.stop_and_verify=stop_and_verify
        self.failed=False

    def _ack(self,nonce,recovered):
        ack=parse(self.receive())
        require(set(ack)=={'nonce','digest','ready'} and ack['nonce']==nonce)
        confirm=self.ledger.confirm_recovered if recovered else self.ledger.confirm_adopted
        confirm(nonce=nonce,pin_digest=ack['digest'],broker_ready=ack['ready'])

    def _verified(self):
        evidence=self.ledger.inspect()
        require(evidence['state']=='ADOPTED' and evidence['generation'] in evidence['matching_generations'])
        return evidence

    def _fail(self):
        self.failed=True
        self.ledger.usable=False
        self.ledger.grant=None
        self.ledger.record=None
        try:stopped=self.stop_and_verify()
        except BaseException as exc:
            raise EgressDenied('Controller failed; shutdown unverified; operator intervention required') from exc
        if stopped is not True:
            raise EgressDenied('Controller failed; shutdown unverified; operator intervention required')

    def renew_progress(self,fd):
        """Only a current usable ledger with exact live kernel evidence renews."""
        require(not self.failed)
        try:
            require(self.ledger.usable and self.ledger.grant is None)
            value,history=self.ledger._current()
            entries=value['rotations']
            state=entries[-1]['state'] if entries else value.get('activation',{}).get('state','INITIAL')
            require(state in ('INITIAL','ADOPTED'))
            self.ledger._verify(history[-1],value['handles'])
            renew(fd)
        except BaseException:
            self._fail()
            raise

    def rotate(self,current,replacement):
        require(not self.failed)
        # Validation before arming does not invoke the broker. After any
        # failure in this operation, conservatively stop and fence the session.
        try:
            nonce=self.ledger.arm(current,replacement)
            self.send(frame({'nonce':nonce}))
            self.send(self.ledger.execute(self.receive()))
            self._ack(nonce,False)
            return self._verified()
        except BaseException:
            self._fail()
            raise

    def adopt_recovery(self,nonce):
        require(not self.failed)
        try:
            self._ack(nonce,True)
            return self._verified()
        except BaseException:
            self._fail()
            raise
