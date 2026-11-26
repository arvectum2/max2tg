"""Offline numeric-pin policy for an isolated Linux broker network namespace.
Render only, never apply firewall rules or authorize a live launch.
Pins must be obtained and reviewed by a separate trusted operator.
"""
import asyncio,ipaddress,json,socket,time
from dataclasses import dataclass
from app.egress_policy import ROLE_HOSTS,approved_host,public_address,EgressDenied

MAX_TTL=3600

@dataclass(frozen=True)
class BrokerPins:
    max_text:tuple[str,...]
    telegram:tuple[str,...]
    expires_at:int

    @classmethod
    def parse(cls,record,*,now=None):
        if type(record) is not dict or set(record)!={'max-text','telegram','expires_at'}:raise EgressDenied('Exact pin bundle required')
        groups=[]
        for role in ROLE_HOSTS:
            values=record[role]
            if type(values) is not list or not 1<=len(values)<=16 or any(type(x) is not str for x in values):raise EgressDenied('Bounded explicit role pins required')
            pins=tuple(public_address(x) for x in values)
            if any(ipaddress.ip_address(x).version!=4 for x in pins) or len(set(pins))!=len(pins):raise EgressDenied('Unique public IPv4 pins required')
            groups.append(tuple(sorted(pins,key=ipaddress.ip_address)))
        if set(groups[0])&set(groups[1]):raise EgressDenied('Overlapping role pins prohibited')
        value=cls(*groups,record['expires_at']);value.remaining(now=now);return value

    def remaining(self,*,now=None):
        import math
        now=time.time() if now is None else now
        if type(now) not in (int,float) or not math.isfinite(now) or type(self.expires_at) is not int or not 1<=self.expires_at-now<=MAX_TTL:raise EgressDenied('Expired or excessive pin lifetime')
        return int(self.expires_at-now)

    def record(self):return {'max-text':list(self.max_text),'telegram':list(self.telegram),'expires_at':self.expires_at}

    def validated(self,*,now=None):return type(self).parse(self.record(),now=now)

    async def connect(self,role,authority,*,dial=None):
        approved_host(role,authority);pins=self.validated();addresses=pins.max_text if role=='max-text' else pins.telegram
        dial=dial or asyncio.open_connection
        # No DNS lookup, proxy environment or arbitrary address fallback.
        return await asyncio.wait_for(dial(addresses[0],443,family=socket.AF_INET,flags=socket.AI_NUMERICHOST),min(5,pins.remaining()))

    def render(self,*,now=None):
        pins=self.validated(now=now);ttl=pins.remaining(now=now)
        addresses=sorted(pins.max_text+pins.telegram,key=ipaddress.ip_address)
        elements=', '.join(f'{ip} timeout {ttl}s' for ip in addresses)
        return f'''table inet m2tg_egress {{
    set api_ipv4 {{
        type ipv4_addr
        flags timeout
        elements = {{ {elements} }}
    }}
    chain output {{
        type filter hook output priority filter; policy drop;
        ip daddr @api_ipv4 tcp dport 443 ct state {{ new, established }} counter accept
        counter drop
    }}
    chain input {{
        type filter hook input priority filter; policy drop;
        ip saddr @api_ipv4 tcp sport 443 ct state established counter accept
        counter drop
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        counter drop
    }}
}}
'''


def read_bundle(text):
    if not isinstance(text,str) or len(text)>8192:raise EgressDenied('Pin bundle size limit')
    def exact(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise EgressDenied('Duplicate pin bundle field')
            result[key]=value
        return result
    return BrokerPins.parse(json.loads(text,object_pairs_hook=exact))
