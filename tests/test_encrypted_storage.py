"""Synthetic scoped AEAD keys, authenticated backup, rotation and runtime entry."""
import sqlite3
from unittest.mock import patch,AsyncMock
import pytest
from app.encrypted_storage import create_key,load_key
from app.encrypted_credentials import install_encrypted_worker,load_encrypted_worker,rotate_encrypted_worker,install_encrypted_bot,load_encrypted_bot,rotate_encrypted_bot
from app.worker_secrets import load_worker_secret,build_max_client
from app.worker_provision import prepare_worker,check_worker
from app.encrypted_backup import encrypt_snapshot,materialize_encrypted,rotate_snapshot
from app.recovery_manifest import RecoveryAnchor
from app.isolated_transport import isolated_max_client,run_isolated_gateway

SID='a'*24;OTHER='b'*24;VALUES={'session_id':SID,'MAX_TOKEN':'synthetic-private-token','MAX_DEVICE_ID':'synthetic-device'}

def folder(root,name):
    p=root/name;p.mkdir(mode=0o700);return p

def new_key(root,name,scope):
    p=root/name;create_key(p,scope);return p

@pytest.mark.asyncio
async def test_worker_scoped_ciphertext_and_rotation(tmp_path):
    secrets=folder(tmp_path,'secrets');keys=folder(tmp_path,'keys');state=folder(tmp_path,'state');channel=folder(tmp_path,'channel');old=new_key(keys,'old','max:'+SID);new=new_key(keys,'new','max:'+SID);other=new_key(keys,'other','max:'+OTHER)
    install_encrypted_worker(secrets,SID,VALUES,old);p=secrets/'max.enc';original=p.read_bytes()
    assert b'synthetic-private-token' not in original and not (secrets/'max.json').exists()
    assert load_worker_secret(secrets,SID,key_path=old)==VALUES
    assert prepare_worker(state,channel,secrets,SID,101,-10001,b'x'*32,key_path=old)
    assert check_worker(state,channel,secrets,SID,101,-10001,b'x'*32,key_path=old)
    with pytest.raises(FileNotFoundError):load_worker_secret(secrets,SID)
    with pytest.raises(ValueError):load_encrypted_worker(secrets,SID,other)
    with pytest.raises(ValueError):load_encrypted_worker(secrets,OTHER,old)
    p.write_bytes(original[:-1]+bytes([original[-1]^1]))
    with pytest.raises(ValueError,match='authentication'):load_encrypted_worker(secrets,SID,old)
    p.write_bytes(original)
    with patch('app.encrypted_storage._write',side_effect=OSError('synthetic interruption')):
        with pytest.raises(OSError):rotate_encrypted_worker(secrets,SID,old,new)
    assert p.read_bytes()==original and load_encrypted_worker(secrets,SID,old)==VALUES
    assert rotate_encrypted_worker(secrets,SID,old,new)==load_key(new,'max:'+SID).kid
    assert p.read_bytes()!=original and old.exists() and new.exists()
    with pytest.raises(ValueError):load_encrypted_worker(secrets,SID,old)
    assert load_encrypted_worker(secrets,SID,new)==VALUES
    client=build_max_client(secrets,SID,key_path=new);assert client.token==VALUES['MAX_TOKEN']
    async with isolated_max_client(tmp_path/'absent.sock',secrets,SID,key_path=new) as client:assert client.token==VALUES['MAX_TOKEN']
    with pytest.raises(FileExistsError):install_encrypted_worker(secrets,SID,VALUES,new)
    (secrets/'max.json').write_text('{}')
    with pytest.raises(PermissionError):load_encrypted_worker(secrets,SID,new)

@pytest.mark.asyncio
async def test_bot_credential_scope_and_runtime_composition(tmp_path):
    keys=folder(tmp_path,'keys');secrets=folder(tmp_path,'secrets');old=new_key(keys,'old','telegram:123456');new=new_key(keys,'new','telegram:123456');token='123456:synthetic-token'
    install_encrypted_bot(secrets,123456,token,old);assert token.encode() not in (secrets/'telegram.enc').read_bytes();assert load_encrypted_bot(secrets,123456,old)==token
    with pytest.raises(ValueError):load_encrypted_bot(secrets,654321,old)
    rotate_encrypted_bot(secrets,123456,old,new);assert load_encrypted_bot(secrets,123456,new)==token
    with pytest.raises(ValueError):load_encrypted_bot(secrets,123456,old)
    from app.capsule_registry import CapsuleRegistry
    import asyncio
    r=CapsuleRegistry(tmp_path/'registry')
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def synthetic(socket,provided,**kw):
        assert provided==token;yield object()
    try:
        with patch('app.isolated_transport.isolated_telegram_bot',synthetic),patch('app.shared_gateway_runtime.SharedGatewayRuntime') as runtime:
            runtime.return_value.run=AsyncMock();await run_isolated_gateway(asyncio.Event(),socket_path=tmp_path/'socket',registry=r,channels={},secret_dir=secrets,secret_key_path=new,bot_id=123456);runtime.return_value.run.assert_awaited_once()
            with pytest.raises(ValueError):await run_isolated_gateway(asyncio.Event(),socket_path=tmp_path/'socket',registry=r,channels={},secret_dir=secrets,secret_key_path=new,bot_id=123456,token=token)
    finally:r.close()


def test_encrypted_snapshot_rotation_tamper_and_dirty(tmp_path):
    root=folder(tmp_path,'snapshot');a=RecoveryAnchor(folder(tmp_path,'anchor'));archive=folder(tmp_path,'archive')/'snapshot.enc';keys=folder(tmp_path,'keys');output=folder(tmp_path,'output')
    db=root/'ledger.sqlite3'
    with sqlite3.connect(db) as c:c.execute('CREATE TABLE receipts(text)');c.execute('INSERT INTO receipts VALUES(?)',('synthetic private content',))
    db.chmod(0o600);record=a.capture(root,{db.name},quiescent=True);scope='backup:'+record['epoch'];old=new_key(keys,'old',scope);new=new_key(keys,'new',scope)
    evidence=encrypt_snapshot(root,archive,old,a);assert evidence['members']==1 and b'synthetic private content' not in archive.read_bytes()
    original=archive.read_bytes();archive.write_bytes(original[:-1]+bytes([original[-1]^1]))
    with pytest.raises(ValueError):materialize_encrypted(archive,old,output,a,name='snapshot-bad')
    assert not list(output.iterdir());archive.write_bytes(original)
    rotate_snapshot(archive,old,new,a)
    with pytest.raises(ValueError):materialize_encrypted(archive,old,output,a,name='snapshot-old')
    restored,e=materialize_encrypted(archive,new,output,a,name='snapshot-ok');assert e['structural_only'] and (restored/db.name).read_bytes()==db.read_bytes()
    with pytest.raises(FileExistsError):materialize_encrypted(archive,new,output,a,name='snapshot-ok')
    a.capture(root,{db.name},quiescent=True)
    with pytest.raises(ValueError):materialize_encrypted(archive,new,output,a,name='snapshot-stale')
    a.mark_dirty()
    with pytest.raises(PermissionError):materialize_encrypted(archive,new,output,a,name='snapshot-dirty')


def test_key_separation_and_unsafe_paths(tmp_path):
    root=folder(tmp_path,'storage');keys=folder(tmp_path,'keys');same=new_key(root,'key','max:'+SID)
    with pytest.raises(PermissionError):install_encrypted_worker(root,SID,VALUES,same)
    key=new_key(keys,'key','max:'+SID);link=keys/'link';link.symlink_to(key)
    with pytest.raises(OSError):load_key(link,'max:'+SID)
    key.chmod(0o644)
    with pytest.raises(PermissionError):load_key(key,'max:'+SID)
