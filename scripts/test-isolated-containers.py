#!/usr/bin/env python3
"""Disposable three-container synthetic SaaS test; no network or real secrets."""
from __future__ import annotations
import argparse,json,subprocess,uuid,time
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);args=p.parse_args()
    root=Path(__file__).resolve().parents[1];prefix='m2tg-iso-test-'+uuid.uuid4().hex[:10];volumes={};names=[]
    def docker(*argv,**kw):return subprocess.run(['docker',*argv],check=True,text=True,capture_output=True,timeout=30,**kw).stdout.strip()
    def command(name,mode,mounts,argv=(),init=False):
        names.append(name);cmd=['docker','run','--name',name,'--pull','never','--label',f'max2tg.test={prefix}','--network','none','--read-only','--security-opt','no-new-privileges','--pids-limit','64','--memory','256m','--mount',f'type=bind,source={root / "app"},target=/src/app,readonly','--mount',f'type=bind,source={root / "tests"},target=/src/tests,readonly','--workdir','/src']
        if not init:cmd+=['--cap-drop','ALL','--user','65532:65532']
        for volume,target in mounts:cmd+=['--mount',f'type=volume,source={volumes[volume]},target=/{target}']
        cmd+=['--entrypoint','python',args.image,'-m','tests.isolated_container_fixture',mode,*argv];return cmd
    try:
        docker('image','inspect',args.image)
        for key in ('registry','channel0','channel1','state0','state1'):
            volumes[key]=docker('volume','create','--label',f'max2tg.test={prefix}',prefix+'-'+key)
        init=subprocess.run(command(prefix+'-init','init',[(k,k) for k in volumes],init=True),check=True,text=True,capture_output=True,timeout=25);sessions=json.loads(init.stdout.strip().splitlines()[-1]);print('Synthetic provisioning PASS',flush=True)
        gateway=subprocess.Popen(command(prefix+'-gateway','gateway',[('registry','registry'),('channel0','channel0'),('channel1','channel1')],[json.dumps(sessions)]),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        # Wait for both dedicated Unix sockets before launching the workers.
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:logs=docker('logs',prefix+'-gateway')
            except subprocess.CalledProcessError:logs=''
            if 'READY' in logs:break
            if gateway.poll() is not None:raise RuntimeError(gateway.communicate()[0])
            time.sleep(.1)
        else:raise RuntimeError('Gateway did not start')
        workers=[]
        for i,(sid,actor,forum) in enumerate(sessions):workers.append(subprocess.Popen(command(prefix+f'-worker{i}','worker',[(f'state{i}','state'),(f'channel{i}','channel')],[sid,str(actor),str(forum)]),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT))
        for i,worker in enumerate(workers):
            out,_=worker.communicate(timeout=23);print(f'Worker {i}: {out.strip()}',flush=True);assert worker.returncode==0 and 'WORKER PASS' in out
        out,_=gateway.communicate(timeout=23);print(out.strip(),flush=True);assert gateway.returncode==0 and 'GATEWAY PASS' in out
        print('CONTAINER ISOLATION PASS',flush=True)
    finally:
        for name in reversed(names):subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        for name in volumes.values():subprocess.run(['docker','volume','rm',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if docker('ps','-aq','--filter',f'label=max2tg.test={prefix}') or docker('volume','ls','-q','--filter',f'label=max2tg.test={prefix}'):raise RuntimeError('Synthetic resources not fully removed: '+prefix)
        print('Disposable containers and volumes removed',flush=True)

if __name__=='__main__':main()
