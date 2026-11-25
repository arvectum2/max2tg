"""CONNECT-only tunnel via dedicated Unix socket; not a production service."""
import asyncio,os,stat
from pathlib import Path
from app.egress_policy import approved_host,connect_public,EgressDenied

LIMIT=8192

async def read_connect(reader):
    raw=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
    if len(raw)>LIMIT:raise EgressDenied('Header too large')
    lines=raw[:-4].decode('ascii').split('\r\n');first=lines[0].split(' ')
    if len(first)!=3 or first[0]!='CONNECT' or first[2] not in ('HTTP/1.0','HTTP/1.1'):raise EgressDenied('CONNECT required')
    authority=first[1];seen=set()
    for line in lines[1:]:
        name,sep,value=line.partition(':');name=name.lower()
        if not sep or name in seen or name not in ('host','user-agent','proxy-connection','connection','accept','accept-encoding'):raise EgressDenied('Unexpected header')
        # aiohttp may omit the default port in Host.
        if name=='host' and value.strip() not in (authority,authority.removesuffix(':443')):raise EgressDenied('Host mismatch')
        if any(ord(c)<32 or ord(c)>126 for c in value):raise EgressDenied('Invalid header')
        seen.add(name)
    return authority

async def close_writer(writer):
    writer.close()
    try:await asyncio.wait_for(writer.wait_closed(),1)
    except (OSError,TimeoutError):pass

async def relay(a,aw,b,bw,*,idle=120):
    async def pump(reader,writer):
        while True:
            chunk=await asyncio.wait_for(reader.read(65536),idle)
            if not chunk:return
            writer.write(chunk);await asyncio.wait_for(writer.drain(),idle)
    tasks=[asyncio.create_task(pump(a,bw)),asyncio.create_task(pump(b,aw))]
    try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

class TunnelServer:
    def __init__(self,role,path,*,connector=None,max_connections=8):
        if type(max_connections) is not int or not 1<=max_connections<=64:raise ValueError('Invalid tunnel limit')
        self.role=role;self.path=Path(path);self.connector=connector or connect_public;self.limit=max_connections;self.server=None;self.tasks=set();self.inode=None
    async def start(self):
        p=self.path.parent;st=p.stat()
        if p.is_symlink() or st.st_uid!=os.getuid() or not stat.S_ISDIR(st.st_mode) or st.st_mode&0o077:raise PermissionError('Private channel required')
        if self.path.exists() or self.path.is_symlink():raise PermissionError('Socket path already exists')
        self.server=await asyncio.start_unix_server(self.handle,str(self.path),limit=LIMIT)
        self.path.chmod(0o600);st=self.path.lstat();self.inode=(st.st_dev,st.st_ino)
    async def handle(self,reader,writer):
        task=asyncio.current_task();up=None;sent=False
        if len(self.tasks)>=self.limit:await close_writer(writer);return
        self.tasks.add(task)
        try:
            authority=await read_connect(reader);approved_host(self.role,authority)
            r,up=await asyncio.wait_for(self.connector(self.role,authority),12)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain();sent=True
            await relay(reader,writer,r,up)
        except (OSError,ValueError,TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):
            if not sent:
                writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                try:await writer.drain()
                except OSError:pass
        finally:
            if up:await close_writer(up)
            await close_writer(writer);self.tasks.discard(task)
    async def close(self):
        if self.server:self.server.close()
        tasks=list(self.tasks)
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.server:await self.server.wait_closed();self.server=None
        if self.inode and self.path.exists():
            st=self.path.lstat()
            if stat.S_ISSOCK(st.st_mode) and (st.st_dev,st.st_ino)==self.inode:self.path.unlink()

class LoopbackProxy:
    """A CONNECT relay for standard aiohttp/httpx proxy support, no DNS/dial."""
    def __init__(self,path):self.path=Path(path);self.server=None;self.tasks=set()
    async def start(self):
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0,limit=LIMIT)
        return f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
    async def handle(self,reader,writer):
        task=asyncio.current_task();up=None
        if len(self.tasks)>=8:await close_writer(writer);return
        self.tasks.add(task)
        try:
            authority=await read_connect(reader)
            r,up=await asyncio.wait_for(asyncio.open_unix_connection(str(self.path),limit=LIMIT),3)
            up.write(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'));await up.drain()
            response=await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),15)
            if response not in (b'HTTP/1.1 200 Connection Established\r\n\r\n',b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'):raise EgressDenied('Invalid broker response')
            writer.write(response);await writer.drain()
            if response.startswith(b'HTTP/1.1 200 '):await relay(reader,writer,r,up)
        except (OSError,ValueError,TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):pass
        finally:
            if up:await close_writer(up)
            await close_writer(writer);self.tasks.discard(task)
    async def close(self):
        if self.server:self.server.close()
        tasks=list(self.tasks)
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.server:await self.server.wait_closed();self.server=None
