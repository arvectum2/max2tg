#!/usr/bin/env python3
"""Real disposable Docker lifecycle smoke. Never use existing tenant volumes."""
from __future__ import annotations
import argparse,tempfile,time,uuid,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import OfflineContainerSupervisor,DockerBackend


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);args=parser.parse_args()
    code=Path(__file__).resolve().parents[1];d=DockerBackend();image=d.call('image','inspect','--format','{{.Id}}',args.image);volumes=[];names=[];init_name=None;s=None
    with tempfile.TemporaryDirectory(prefix='m2tg-orch-') as directory:
        root=Path(directory);now=[time.time()]
        def open_supervisor():return OfflineContainerSupervisor(root,image,code,clock=lambda:now[0])
        def until(check,timeout=12):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                s.tick()
                if check():return
                time.sleep(.2)
            raise RuntimeError('Lifecycle condition not met: '+str([(sid,s.row(sid)['status']) for sid in ids]))
        s=open_supervisor();owner=s.owner;ids=[uuid.uuid4().hex[:24],uuid.uuid4().hex[:24]]
        try:
            for i,sid in enumerate(ids):
                vs=[]
                for kind in ('state','channel','secret'):
                    n=f'm2tg-orch-{owner}-{i}-{kind}';d.call('volume','create','--label',f'max2tg.offline={owner}',n);volumes.append(n);vs.append(n)
                init_name=f'm2tg-orch-init-{owner}-{i}'
                init=['run','--rm','--name',init_name,'--pull','never','--network','none','--read-only','--label',f'max2tg.offline={owner}']
                for n,k in zip(vs,('state','channel','secret')):init+=['--mount',f'type=volume,source={n},target=/{k}']
                d.call(*init,'--entrypoint','python',image,'-c','import os; [(os.chmod(p,0o700),os.chown(p,65532,65532)) for p in ("/state","/channel","/secret")]')
                s.register(sid,*vs);names.append(s.name(sid));s.desired(sid,True)
            until(lambda:all(s.row(x)['status']=='HEALTHY' for x in ids));a,b=ids;aid=s.row(a)['cid'];bid=s.row(b)['cid'];print('Both workers HEALTHY with verified isolation and quotas',flush=True)
            d.call('exec',aid,'python','-c','from pathlib import Path;Path("/state/crash").touch()')
            until(lambda:s.row(a)['status']=='BACKOFF');assert s.row(b)['status']=='HEALTHY'
            s.close();s=open_supervisor();assert s.owner==owner and s.row(a)['attempts']==1 and s.row(a)['status']=='BACKOFF';now[0]+=3
            until(lambda:s.row(a)['attempts']==2 and s.row(a)['status']=='HEALTHY');assert s.row(a)['cid']==aid and s.row(b)['cid']==bid and s.row(b)['attempts']==1
            assert d.call('exec',aid,'python','-c','from pathlib import Path;print(Path("/state/starts").read_text())')=='2';print('Crash recovered after supervisor handoff; other worker unchanged',flush=True)
            s.desired(a,False);until(lambda:s.row(a)['status']=='STOPPED');assert s.row(b)['status']=='HEALTHY'
            s.desired(b,False);until(lambda:s.row(b)['status']=='STOPPED');print('Per-tenant stop verified; volumes preserved',flush=True)
        finally:
            if s:s.close()
            # Only dispose resources carrying this run's exact owner label.
            for n in names+([init_name] if init_name else []):
                c=d.inspect(n)
                if c and c['Config']['Labels'].get('max2tg.offline')==owner:d.call('rm','-f',c['Id'])
            for n in volumes:
                if d.volume(n).get('Labels',{}).get('max2tg.offline')==owner:d.call('volume','rm',n)
            assert not d.call('ps','-aq','--filter',f'label=max2tg.offline={owner}') and not d.call('volume','ls','-q','--filter',f'label=max2tg.offline={owner}')
    print('SUPERVISOR SMOKE PASS; disposable resources removed',flush=True)

if __name__=='__main__':main()
