#!/usr/bin/env python3
"""Create only held synthetic containers and verify actual mount placement."""
import argparse,json,secrets,subprocess,tempfile,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.deployment_plan import build_plan,verify_stopped_container,plan_digest,validate_bind_sources

def docker(*args):return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,timeout=20).stdout.strip()

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);a=p.parse_args();image=docker('image','inspect','--format','{{.Id}}',a.image);deployment=secrets.token_hex(12)
    # Use canonical /Users or /tmp via resolved current checkout for Docker Desktop.
    with tempfile.TemporaryDirectory(prefix='placement-',dir=Path(__file__).resolve().parents[1]) as td:
        root=Path(td).resolve();plan=build_plan(deployment,image,['a'*24,'b'*24],data=root/'data',keys=root/'keys',backup=root/'backup');path=root/'compose.json';path.write_text(json.dumps(plan))
        for s in plan['services'].values():
            for m in s['volumes']:Path(m['source']).mkdir(parents=True,exist_ok=True,mode=0o700)
        assert validate_bind_sources(plan)==14
        cmd=('compose','-f',str(path),'--profile','placement-only');docker(*cmd,'config','--quiet')
        try:
            docker(*cmd,'create','--no-build','--pull','never')
            for s in plan['services'].values():
                c=json.loads(docker('inspect',s['container_name']))[0];print(verify_stopped_container(s,c),flush=True)
            print('PLACEMENT PASS',plan_digest(plan),flush=True)
        finally:
            # Exact synthetic label and names only; never down unrelated projects.
            for s in plan['services'].values():
                try:c=json.loads(docker('inspect',s['container_name']))[0]
                except subprocess.CalledProcessError:continue
                if c['Config']['Labels'].get('max2tg.placement')==deployment:docker('rm','-f',c['Id'])
            assert not docker('ps','-aq','--filter',f'label=max2tg.placement={deployment}')
if __name__=='__main__':main()
