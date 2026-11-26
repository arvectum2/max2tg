#!/usr/bin/env python3
"""Disposable real Docker acceptance of the operator staging CLI; never start."""
import argparse,json,secrets,subprocess,tempfile,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.deployment_plan import build_plan
from app.container_supervisor import DockerBackend
from app.operator_stage import _inspect


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);a=p.parse_args();d=DockerBackend();image=d.call('image','inspect','--format','{{.Id}}',a.image);owner=secrets.token_hex(12)
    with tempfile.TemporaryDirectory(prefix='operator-stage-',dir=Path(__file__).resolve().parents[1]) as td:
        root=Path(td).resolve();state=root/'operator';state.mkdir(mode=0o700)
        cfg=dict(deployment=owner,image=image,sids=['a'*24,'b'*24],data=str(root/'data'),keys=str(root/'keys'),backup=str(root/'backup'));plan=build_plan(**cfg)
        for s in plan['services'].values():
            for m in s['volumes']:Path(m['source']).mkdir(parents=True,exist_ok=True,mode=0o700)
        def cli(*args):
            r=subprocess.run([sys.executable,'-m','app.operator_stage','--state',str(state),*args],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=90)
            if r.returncode:raise RuntimeError(r.stderr[-2000:])
            return json.loads(r.stdout)
        args=['prepare',*[z for k in ('deployment','image','data','keys','backup') for z in ('--'+k,cfg[k])],*[z for sid in cfg['sids'] for z in ('--sid',sid)]]
        try:
            first=cli(*args);assert first['state']=='HELD' and first['containers']==4 and first['live_launch_authorized'] is False
            ids={role:_inspect(d,s) for role,s in plan['services'].items()};assert len(set(ids.values()))==4 and None not in ids.values()
            assert cli('status')==first and cli(*args)==first
            assert {role:_inspect(d,s) for role,s in plan['services'].items()}==ids
            print('OPERATOR STAGE PASS '+json.dumps(first,sort_keys=True),flush=True)
        finally:
            # Exact names, label and full held policy only; fixture-owned resources.
            for s in plan['services'].values():
                c=d.inspect(s['container_name'])
                if c and c['Config']['Labels'].get('max2tg.placement')==owner:
                    cid=_inspect(d,s);d.call('rm',cid)
            assert not d.call('ps','-aq','--filter','label=max2tg.placement='+owner)
            print('OPERATOR CLEANUP PASS',flush=True)

if __name__=='__main__':main()
