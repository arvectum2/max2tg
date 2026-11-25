"""Synthetic external credentials and metadata-only registry contracts."""
from pathlib import Path
import pytest
from app.capsule_registry import CapsuleRegistry
from app.worker_secrets import install_worker_secret,load_worker_secret,build_max_client
from app.shared_gateway_runtime import SharedGatewayRuntime
from tests.test_shared_runtime import bot


def values(sid):return {'session_id':sid,'MAX_TOKEN':'synthetic-private-token','MAX_DEVICE_ID':'synthetic-device'}


def test_new_external_capsule_never_stores_max_secrets(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');sid=r.create_capsule(r.create_account());folder=tmp_path/'secret';folder.mkdir(mode=0o700)
    try:
        install_worker_secret(folder,sid,values(sid));r.configure_external_shared(sid,-12345);r.set_active(sid,True)
        assert r.get(sid)['credential_mode']=='external' and r.get(sid)['telegram_mode']=='shared'
        assert not (r.capsule_dir(sid)/'credentials.json').exists()
        with pytest.raises(PermissionError):r.credentials(sid)
        assert all(b'synthetic-private-token' not in p.read_bytes() for p in r.root.rglob('*') if p.is_file())
        assert build_max_client(folder,sid,factory=lambda **kw:kw)=={'token':'synthetic-private-token','device_id':'synthetic-device','debug':False}
        with pytest.raises(PermissionError):SharedGatewayRuntime(r,bot())
        r.set_active(sid,False)
        with pytest.raises(ValueError):r.configure_external_shared(sid,-54321)
        with pytest.raises(ValueError):r.configure(sid,{},telegram_mode='shared')
    finally:r.close()


def test_legacy_secret_is_not_migrated_or_deleted(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');sid=r.create_capsule(r.create_account())
    try:
        data={'MAX_TOKEN':'legacy','MAX_DEVICE_ID':'device','TG_CHAT_ID':'-12345','TG_ADMIN_USER_ID':'42'};r.configure(sid,data,telegram_mode='shared');p=r.capsule_dir(sid)/'credentials.json';before=p.read_bytes()
        with pytest.raises(ValueError):r.configure_external_shared(sid,-12345)
        assert p.read_bytes()==before and r.credentials(sid)==data
    finally:r.close()


def test_secret_wrong_identity_fields_permissions_and_links(tmp_path):
    folder=tmp_path/'secret';folder.mkdir(mode=0o700);sid='a'*24;other='b'*24
    with pytest.raises(ValueError):install_worker_secret(folder,sid,dict(values(sid),TG_BOT_TOKEN='not-allowed'))
    install_worker_secret(folder,sid,values(sid));p=folder/'max.json';before=p.read_bytes()
    with pytest.raises(FileExistsError):install_worker_secret(folder,sid,values(sid))
    assert p.read_bytes()==before
    with pytest.raises(ValueError):load_worker_secret(folder,other)
    p.chmod(0o644)
    with pytest.raises(PermissionError):load_worker_secret(folder,sid)
    p.chmod(0o600);link=tmp_path/'link';link.hardlink_to(p)
    with pytest.raises(PermissionError):load_worker_secret(folder,sid)
    link.unlink();p.unlink();p.symlink_to(Path('/nonexistent'))
    with pytest.raises(OSError):load_worker_secret(folder,sid)


def test_external_only_gateway_rejects_legacy_and_missing_channel(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');sid=r.create_capsule(r.create_account());channel=tmp_path/'channel';channel.mkdir(mode=0o700)
    try:
        r.configure_external_shared(sid,-12345);r.set_active(sid,True)
        with pytest.raises(PermissionError,match='missing channel'):SharedGatewayRuntime(r,bot(),channels={},external_only=True)
        legacy=r.create_capsule(r.create_account());r.configure(legacy,{'MAX_TOKEN':'legacy','MAX_DEVICE_ID':'device','TG_CHAT_ID':'-54321','TG_ADMIN_USER_ID':'43'},telegram_mode='shared')
        with pytest.raises(PermissionError,match='Legacy credentials'):SharedGatewayRuntime(r,bot(),channels={sid:channel},external_only=True)
    finally:r.close()
