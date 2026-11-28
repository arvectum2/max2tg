"""Restricted broker for a separately protected network namespace.
No credentials, DNS, firewall installation or live launch authority.
Reviewed generations; all listeners and tunnels close on expiry or ambiguity.
"""
import asyncio,re,time
from pathlib import Path
from app.egress_policy import EgressDenied
from app.egress_tunnel import TunnelServer,close_writer
from app.linux_egress_policy import BrokerPins
from app.linux_pin_rotation import rotation_batch
from app.recovery_manifest import _private_dir

SID=re.compile(r'[0-9a-f]{24}')


class PinnedBroker:
    def __init__(self,pins,channels,*,dial=None):
        if type(pins) is not BrokerPins:raise ValueError('Reviewed pin bundle required')
        self.pins=pins.validated()
        if type(channels) is not dict or 'telegram' not in channels or not 2<=len(channels)<=33 or any(k!='telegram' and (type(k) is not str or not SID.fullmatch(k)) for k in channels):raise ValueError('Explicit gateway and 1–32 tenant channels required')
        paths={};identities=set()
        for role,value in channels.items():
            p=Path(value)
            if not p.is_absolute() or p.resolve()!=p:raise PermissionError('Canonical private channel required')
            p=_private_dir(p);st=p.stat();identity=(st.st_dev,st.st_ino)
            if identity in identities or any(p.is_relative_to(q) or q.is_relative_to(p) for q in paths.values()):raise PermissionError('Aliased or overlapping channels')
            if any(p.iterdir()):raise PermissionError('Fresh empty broker channel required')
            paths[role]=p;identities.add(identity)
        self._dial=dial;self._deadline=time.monotonic()+self.pins.remaining();self._accepting=False;self._used=False;self.ready=asyncio.Event()
        self._generation=0;self._rotating=False;self._failed=False;self._running=False
        self._streams={};self._stop=None
        self.servers={k:TunnelServer('telegram' if k=='telegram' else 'max-text',p/'proxy.sock',connector=self._connect,max_connections=16 if k=='telegram' else 8) for k,p in paths.items()}

    def _lifetime(self):
        if self._failed or self._deadline is None or time.monotonic()>=self._deadline:
            raise EgressDenied('Broker pin generation closed')
        self.pins.remaining()

    def _valid(self):
        self._lifetime()
        if not self._accepting or (self._stop is not None and self._stop.is_set()):
            raise EgressDenied('Broker admission closed')

    def _prune(self):
        self._streams={w:pair for w,pair in self._streams.items() if not w.is_closing()}

    def _abort_streams(self, keep=None):
        self._prune()
        for writer,(role,ip) in list(self._streams.items()):
            if keep is None or ip not in keep[role]:
                # Abort, not a graceful flush of queued revoked bytes.
                writer.transport.abort()
                self._streams.pop(writer,None)

    async def _connect(self,role,authority):
        self._valid()
        generation=self._generation;pins=self.pins
        addresses=pins.max_text if role=='max-text' else pins.telegram
        reader,writer=await pins.connect(role,authority,dial=self._dial)
        try:
            self._valid()
            if generation!=self._generation:
                raise EgressDenied('Dial crossed pin generation')
        except BaseException:
            writer.close()
            await close_writer(writer)
            raise
        self._prune()
        self._streams[writer]=(role,addresses[0])
        return reader,writer

    async def rotate(self,replacement,*,verified_update):
        """Trusted async controller, no privilege or external launch authority.

        Pause admission, fence in-flight dials, abort revoked streams before
        the single kernel transaction. On ambiguous outcome never reopen.
        The caller must own the exact namespace and return verified evidence.
        """
        if not self._running or self._rotating or not self.ready.is_set() or self._stop.is_set():
            raise EgressDenied('Broker not ready for rotation')
        self._valid()
        current=self.pins
        rotation_batch(current,replacement,now=time.time())
        old_deadline=self._deadline
        new_deadline=time.monotonic()+replacement.remaining()
        self._rotating=True;self._accepting=False;self.ready.clear()
        self._generation+=1
        try:
            self._abort_streams({'max-text':replacement.max_text,'telegram':replacement.telegram})
            remaining=min(5,old_deadline-time.monotonic())
            if remaining<=0:raise EgressDenied('Rotation deadline elapsed')
            result=await asyncio.wait_for(verified_update(current,replacement),remaining)
            expected={'verified':True,'pins':len(replacement.max_text)+len(replacement.telegram),
                      'rotated':True,'live_launch_authorized':False}
            if (type(result) is not dict or result!=expected or result['verified'] is not True
                    or result['rotated'] is not True or result['live_launch_authorized'] is not False
                    or type(result['pins']) is not int):
                raise EgressDenied('Missing verified kernel rotation evidence')
            self._lifetime()
            if not self._running or self._stop.is_set() or time.monotonic()>=new_deadline:
                raise EgressDenied('Broker stopped during rotation')
            self.pins=replacement.validated()
            self._deadline=min(new_deadline,time.monotonic()+self.pins.remaining())
            self._accepting=True;self._valid();self.ready.set()
        except BaseException:
            self._failed=True;self._accepting=False;self.ready.clear()
            self._abort_streams()
            raise
        finally:
            self._rotating=False

    async def serve(self,stop):
        if self._used:raise PermissionError('Pin generation cannot be restarted')
        self._used=True;self._stop=stop;self._deadline=min(self._deadline,time.monotonic()+self.pins.remaining())
        try:
            for server in self.servers.values():
                if stop.is_set():return 'STOPPED'
                await server.start()
            self._running=True;self._accepting=True;self._valid();self.ready.set()
            while not stop.is_set():
                try:self._lifetime()
                except EgressDenied:return 'ROTATION_FAILED' if self._failed else 'EXPIRED'
                try:await asyncio.wait_for(stop.wait(),min(.1,max(.001,self._deadline-time.monotonic())))
                except TimeoutError:pass
            return 'STOPPED'
        finally:
            self._running=False;self._accepting=False;self.ready.clear()
            self._abort_streams()
            await asyncio.gather(*(s.close() for s in self.servers.values()))
