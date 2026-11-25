#!/usr/bin/env python3
"""Disposable no-network TLS tunnel test with a temporary synthetic certificate."""
import argparse,subprocess,tempfile,uuid,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import DockerBackend

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);args=p.parse_args();d=DockerBackend();root=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',args.image);owner=uuid.uuid4().hex[:16];prefix='m2tg-net-'+owner;vol=[];names=[]
    with tempfile.TemporaryDirectory(prefix='m2net-') as td:
        folder=Path(td);cert=folder/'cert';cert.mkdir();client=folder/'client';client.mkdir()
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],check=True,capture_output=True)
        # Disposable synthetic key only; non-root Docker fixture needs read access.
        (cert/'key.pem').chmod(0o644);(client/'cert.pem').write_bytes((cert/'cert.pem').read_bytes());folder.chmod(0o755)
        def cmd(name,role,mounts,certdir=None,init=False,detach=False):
            names.append(name);a=['run','--name',name,'--pull','never','--label',f'max2tg.nettest={owner}','--network','none','--read-only','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--mount',f'type=bind,source={root / "app"},target=/src/app,readonly','--mount',f'type=bind,source={root / "tests"},target=/src/tests,readonly','--workdir','/src']
            if not init:a+=['--user','65532:65532','--cap-drop','ALL']
            if detach:a+=['-d']
            for n,target in mounts:a+=['--mount',f'type=volume,source={n},target=/{target}'+(',readonly' if target=='secret' and not init else '')]
            if certdir:a+=['--mount',f'type=bind,source={certdir},target=/cert,readonly']
            return a+['--entrypoint','python',image,'-m','tests.network_tunnel_fixture',role]
        try:
            for i in range(3):vol.append(d.call('volume','create','--label',f'max2tg.nettest={owner}',prefix+f'-{i}'))
            both=[(n,f'channel{i}') for i,n in enumerate(vol[:2])];d.call(*cmd(prefix+'-init','init',both+[(vol[2],'secret')],init=True));broker=d.call(*cmd(prefix+'-broker','broker',both,cert,detach=True))
            for _ in range(40):
                if 'READY' in d.call('logs',broker):break
                time.sleep(.1)
            else:raise RuntimeError('Broker not ready')
            for i,role in enumerate(('max-text','telegram')):print(d.call(*cmd(prefix+'-'+role,role,[(vol[i],'channel')]+([(vol[2],'secret')] if i==0 else []),client)),flush=True)
            assert d.call('wait',broker)=='0';print(d.call('logs',broker),flush=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout,exc.stderr,flush=True)
            for name in names:
                try:print(name,d.call("logs",name),flush=True)
                except subprocess.CalledProcessError:pass
            raise
        finally:
            for name in reversed(names):
                c=d.inspect(name)
                if c and c['Config']['Labels'].get('max2tg.nettest')==owner:d.call('rm','-f',c['Id'])
            for name in vol:
                if d.volume(name).get('Labels',{}).get('max2tg.nettest')==owner:d.call('volume','rm',name)
            assert not d.call('ps','-aq','--filter',f'label=max2tg.nettest={owner}') and not d.call('volume','ls','-q','--filter',f'label=max2tg.nettest={owner}')
    print('NETWORK TUNNEL PASS; disposable resources removed',flush=True)
if __name__=='__main__':main()
