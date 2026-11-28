#!/usr/bin/env python3
"""Disposable Linux namespace, never connects to actual public IPs."""
import argparse,secrets,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import DockerBackend


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);a=p.parse_args();d=DockerBackend();root=Path(__file__).resolve().parents[1];owner=secrets.token_hex(12);name='m2tg-pin-test-'+owner;label='max2tg.pintest='+owner;image=d.call('image','inspect','--format','{{.Id}}',a.image)
    with tempfile.TemporaryDirectory(prefix='m2pin-cert-') as td:
        cert=Path(td);subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],check=True,capture_output=True);cert.chmod(0o755);(cert/'key.pem').chmod(0o644)
        try:
            cid=d.call('run','-d','--name',name,'--label',label,'--network','none','--read-only','--pull','never','--cap-drop','ALL','--cap-add','NET_ADMIN','--security-opt','no-new-privileges','--memory','128m','--pids-limit','32','--sysctl','net.ipv4.ip_unprivileged_port_start=0','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777','--mount',f'type=bind,source={root / "app"},target=/app/app,readonly','--mount',f'type=bind,source={root / "tests"},target=/app/tests,readonly','--mount',f'type=bind,source={cert},target=/cert,readonly','--entrypoint','python',image,'-m','tests.pinned_broker_linux_fixture','setup')
            observed=d.inspect(name);assert observed['Id']==cid and observed['HostConfig']['NetworkMode']=='none' and not observed['HostConfig']['Privileged']
            for _ in range(40):
                if 'SETUP READY' in d.call('logs',cid):break
                time.sleep(.1)
            else:raise RuntimeError('Isolated loopback setup failed')
            print(d.call('exec','--user','65532:65532',cid,'python','-m','tests.pinned_broker_linux_fixture','exercise'),flush=True)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout,exc.stderr,flush=True);raise
        finally:
            c=d.inspect(name)
            if c and c['Config']['Labels'].get('max2tg.pintest')==owner:d.call('rm','-f',c['Id'])
            assert not d.call('ps','-aq','--filter','label='+label)
            print('PINNED NUMERIC TLS CLEANUP PASS',flush=True)

if __name__=='__main__':main()
