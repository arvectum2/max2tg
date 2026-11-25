import os
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from app.host_storage_preflight import inspect_storage,StorageDenied,parse_mountinfo

ROOTS=('/srv/max2tg/data','/srv/max2tg/backup','/run/max2tg-keys')
SWAP='Filename Type Size Used Priority\n'
MOUNTS='''11 1 253:0 / /srv/max2tg/data rw,nosuid,nodev,noexec - ext4 /dev/mapper/data rw
12 1 253:1 / /srv/max2tg/backup rw,nosuid,nodev,noexec - xfs /dev/mapper/backup rw
13 1 0:42 / /run/max2tg-keys rw,nosuid,nodev,noexec - tmpfs tmpfs rw
'''

def inspect(tmp_path,mounts=MOUNTS,swaps=SWAP,uuids=None):
    for i in range(2):
        p=tmp_path/f'253:{i}'/'dm';p.mkdir(parents=True,exist_ok=True);(p/'uuid').write_text(uuids[i] if uuids else 'CRYPT-LUKS2-'+str(i)*32+f'-test{i}')
    devs=dict(zip(ROOTS,[(253,0),(253,1),(0,42)]))
    return inspect_storage(*ROOTS,mountinfo=mounts,sys_block=tmp_path,swaps=swaps,check_dir=lambda p:(Path(p),NS(st_dev=os.makedev(*devs[p]))))


def test_only_local_storage_pass_not_launch_authority(tmp_path):
    result=inspect(tmp_path);assert result['local_storage_checks']=='PASS' and result['live_launch_authorized'] is False and len(result['manual_gates'])==4
    assert parse_mountinfo(MOUNTS.replace('/srv/max2tg/data','/srv/max2tg/data\\040space'))[0].point.endswith('data space')

@pytest.mark.parametrize('mounts',[
    MOUNTS.replace('253:0','8:0'),
    MOUNTS.replace('rw,nosuid,nodev,noexec','rw,nosuid,nodev',1),
    MOUNTS.replace('253:0 / /srv','253:0 /subroot /srv'),
    MOUNTS+'14 11 0:43 / /srv/max2tg/data/nested rw - tmpfs tmpfs rw\n',
    MOUNTS+MOUNTS.splitlines()[0]+'\n',
    MOUNTS.replace('tmpfs tmpfs rw','ext4 /dev/sda rw'),
    MOUNTS.replace('ext4 /dev/mapper/data','overlay overlay'),
    MOUNTS+'14 1 253:0 / /other rw - ext4 /dev/mapper/data rw\n',
])
def test_mount_mismatch_refused(tmp_path,mounts):
    with pytest.raises(StorageDenied):inspect(tmp_path,mounts)

def test_swap_refused(tmp_path):
    with pytest.raises(StorageDenied):inspect(tmp_path,swaps=SWAP+'/swap file 1024 1 -2\n')
    with pytest.raises(StorageDenied):inspect(tmp_path,swaps='')

@pytest.mark.parametrize('uuids',[
    ['LVM-not-encrypted','CRYPT-LUKS2-'+'a'*32+'-test1'],
    ['CRYPT-LUKS2-'+'a'*32+'-test0','CRYPT-LUKS2-'+'a'*32+'-test1'],
])
def test_non_luks_or_duplicate_header_refused(tmp_path,uuids):
    with pytest.raises(StorageDenied):inspect(tmp_path,uuids=uuids)
