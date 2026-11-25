"""Exact-host HTTPS tunnel policy, with numeric-address DNS pinning.

Offline component only. No media domains or production runtime activation.
"""
import asyncio,ipaddress,re,socket

ROLE_HOSTS={'max-text':frozenset({'api.oneme.ru'}),'telegram':frozenset({'api.telegram.org'})}
AUTHORITY=re.compile(r'([a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?):443',re.ASCII)

class EgressDenied(PermissionError):pass

def approved_host(role,authority):
    if type(authority) is not str or role not in ROLE_HOSTS:raise EgressDenied('Unapproved role or destination')
    match=AUTHORITY.fullmatch(authority)
    if not match or match[1] not in ROLE_HOSTS[role]:raise EgressDenied('Unapproved destination')
    return match[1]

def public_address(value):
    try:ip=ipaddress.ip_address(value)
    except ValueError:raise EgressDenied('Invalid DNS answer') from None
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or getattr(ip,'ipv4_mapped',None) is not None or getattr(ip,'sixtofour',None) is not None or getattr(ip,'teredo',None) is not None or '%' in str(value):raise EgressDenied('Non-public DNS answer')
    if ip.version==6 and ip in ipaddress.ip_network('64:ff9b::/96'):raise EgressDenied('Translation prefix prohibited')
    return str(ip)

async def connect_public(role,authority,*,resolve=None,dial=None):
    host=approved_host(role,authority);loop=asyncio.get_running_loop()
    resolve=resolve or loop.getaddrinfo;dial=dial or asyncio.open_connection
    records=await asyncio.wait_for(resolve(host,443,type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP),5)
    if not records or len(records)>32:raise EgressDenied('Unexpected DNS answer count')
    addresses=[]
    for family,kind,proto,_,sockaddr in records:
        if family not in (socket.AF_INET,socket.AF_INET6) or kind!=socket.SOCK_STREAM or proto!=socket.IPPROTO_TCP or sockaddr[1]!=443 or len(sockaddr)!=(2 if family==socket.AF_INET else 4) or len(sockaddr)==4 and sockaddr[3]!=0:raise EgressDenied('Invalid DNS record')
        addr=public_address(sockaddr[0]);addresses.append((family,addr))
    # Validate every answer; never resolve a second time on connect.
    family,addr=addresses[0]
    return await asyncio.wait_for(dial(addr,443,family=family,flags=socket.AI_NUMERICHOST),5)
