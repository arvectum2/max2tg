"""Restricted broker for a separately protected network namespace.
No credentials, DNS, firewall installation or live launch authority.
One immutable pin generation; all listeners and tunnels close on expiry.
"""
import asyncio,re,time
from pathlib import Path
from app.egress_policy import EgressDenied
from app.egress_tunnel import TunnelServer,close_writer
from app.linux_egress_policy import BrokerPins
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
        self.servers={k:TunnelServer('telegram' if k=='telegram' else 'max-text',p/'proxy.sock',connector=self._connect,max_connections=16 if k=='telegram' else 8) for k,p in paths.items()}

    def _valid(self):
        if not self._accepting or self._deadline is None or time.monotonic()>=self._deadline:raise EgressDenied('Broker pin generation closed')
        self.pins.remaining()

    async def _connect(self,role,authority):
        self._valid()
        reader,writer=await self.pins.connect(role,authority,dial=self._dial)
        try:self._valid()
        except BaseException:
            await close_writer(writer);raise
        return reader,writer

    async def serve(self,stop):
        if self._used:raise PermissionError('Pin generation cannot be restarted')
        self._used=True;self._deadline=min(self._deadline,time.monotonic()+self.pins.remaining())
        try:
            for server in self.servers.values():
                if stop.is_set():return 'STOPPED'
                await server.start()
            self._accepting=True;self._valid();self.ready.set()
            while not stop.is_set():
                try:self._valid()
                except EgressDenied:return 'EXPIRED'
                try:await asyncio.wait_for(stop.wait(),min(.1,max(.001,self._deadline-time.monotonic())))
                except TimeoutError:pass
            return 'STOPPED'
        finally:
            self._accepting=False;self.ready.clear()
            await asyncio.gather(*(s.close() for s in self.servers.values()))
