#!/usr/bin/env python3
"""Experimental diagnostic only: routed TLS acceptance is NOT established."""
import argparse,json,secrets,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import DockerBackend

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);a=p.parse_args();d=DockerBackend();root=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',a.image);owner=secrets.token_hex(12);net='m2tg-gate-'+owner;label='max2tg.gatetest='+owner;names=[net+'-server',net+'-broker']
    with tempfile.TemporaryDirectory(prefix='m2gate-') as td:
        cert=Path(td);subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],capture_output=True,check=True);cert.chmod(0o755);(cert/'key.pem').chmod(0o644)
        nid=None
        def launch(name,*args):
            cid=d.call('run','-d','--name',name,'--label',label,'--network',net,'--read-only','--pull','never','--cap-drop','ALL','--cap-add','NET_ADMIN','--security-opt','no-new-privileges','--memory','128m','--pids-limit','32','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777','--mount',f'type=bind,source={root / "app"},target=/app/app,readonly','--mount',f'type=bind,source={root / "tests"},target=/app/tests,readonly','--mount',f'type=bind,source={cert},target=/cert,readonly','--entrypoint','python',image,'-m','tests.linux_namespace_diagnostic',*args)
            c=d.inspect(name);assert c['Id']==cid and set(c['NetworkSettings']['Networks'])=={net} and not c['HostConfig']['Privileged'] and not c['HostConfig']['PortBindings'];return cid,c['NetworkSettings']['Networks'][net]['IPAddress']
        def wait(cid,marker):
            for _ in range(80):
                if marker in d.call('logs',cid):return
                time.sleep(.1)
            raise RuntimeError('Fixture preparation failed')
        try:
            nid=d.call('network','create','--internal','--driver','bridge','--label',label,net);v=json.loads(d.call('network','inspect',nid))[0];assert v['Internal'] is True and v['Driver']=='bridge' and not v['Containers']
            sid,peer=launch(names[0],'server');wait(sid,'SERVER READY');expiry=str(int(time.time())+20);bid,_=launch(names[1],'prepare',peer,expiry);wait(bid,'GATE READY')
            v=json.loads(d.call('network','inspect',nid))[0];assert set(v['Containers'])=={sid,bid}
            print(subprocess.run(['docker','exec','--user','65532:65532',bid,'python','-m','tests.linux_namespace_diagnostic','exercise',expiry],check=True,capture_output=True,text=True,timeout=30).stdout,flush=True)
            data=json.loads(d.call('exec',bid,'nft','-j','list','chain','inet','m2tg_egress','output'));counts=[e['counter']['packets'] for i in data['nftables'] if 'rule' in i for e in i['rule']['expr'] if 'counter' in e];assert counts[0]>2 and counts[1]>=2;print('NAMESPACE KERNEL PASS '+json.dumps({'allowed_packets':counts[0],'blocked_packets':counts[1]}),flush=True)
        except (subprocess.CalledProcessError,RuntimeError) as e:
            print(str(e),getattr(e,'stderr',''),flush=True)
            for n in names:
                if d.inspect(n):print(d.call('logs',n),flush=True)
            raise
        finally:
            for n in reversed(names):
                c=d.inspect(n)
                if c and c['Config']['Labels'].get('max2tg.gatetest')==owner:d.call('rm','-f',c['Id'])
            if nid:
                v=json.loads(d.call('network','inspect',nid))[0];assert v['Labels'].get('max2tg.gatetest')==owner and not v['Containers'];d.call('network','rm',nid)
            assert not d.call('ps','-aq','--filter','label='+label) and not d.call('network','ls','-q','--filter','label='+label)
            print('NAMESPACE CLEANUP PASS',flush=True)

if __name__=='__main__':main()
