"""P2 capsule registry tests with synthetic data only."""
import pytest
from pathlib import Path
from app.capsule_registry import CapsuleRegistry

def test_capsule_account_and_session(tmp_path):
    reg=CapsuleRegistry(tmp_path/'capsules')
    user=reg.create_account()
    sid=reg.create_capsule(user)
    assert reg.get(sid)['user_id']==user
    assert reg.get(sid)['desired_active']==0
    reg.close()

from app.capsule_supervisor import CapsuleSupervisor, build_worker_env, observe_resources, lock_held
import os
import json
import time
import stat


def credentials(i):
    return dict(MAX_TOKEN=f"dummy-{i}",MAX_DEVICE_ID=f"device-{i}",
                TG_BOT_TOKEN=f"bot-{i}",TG_CHAT_ID=str(-10000000000-i),
                TG_ADMIN_USER_ID=str(10000+i))


def provision(reg,i):
    uid=reg.create_account()
    sid=reg.create_capsule(uid)
    reg.configure(sid,credentials(i))
    return uid,sid


def ready(predicate):
    finish=time.monotonic()+5
    while time.monotonic()<finish:
        if predicate():return True
        time.sleep(0.05)
    return False


def test_private_per_tenant_state_and_no_credentials_in_db(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    folder=reg.capsule_dir(sid)
    assert stat.S_IMODE(folder.stat().st_mode)==0o700
    for name in ("state","logs","browser-profile"):
        assert stat.S_IMODE((folder/name).stat().st_mode)==0o700
    path=folder/"credentials.json"
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert reg.credentials(sid)==credentials(1)
    db=(reg.root/"capsules.sqlite3").read_bytes()
    assert credentials(1)["MAX_TOKEN"].encode() not in db
    reg.close()


def test_no_duplicate_bot_or_group(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,a=provision(reg,1)
    _,b=provision(reg,2)
    values=credentials(2);values["TG_CHAT_ID"]=credentials(1)["TG_CHAT_ID"]
    with pytest.raises(ValueError):
        reg.configure(b,values)
    values=credentials(2);values["TG_BOT_TOKEN"]=credentials(1)["TG_BOT_TOKEN"]
    with pytest.raises(ValueError):
        reg.configure(b,values)
    reg.close()


def test_credentials_cannot_control_runtime_paths(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    uid=reg.create_account();sid=reg.create_capsule(uid)
    values=credentials(1);values["STATE_DIR"]="/tmp/other"
    with pytest.raises(ValueError):
        reg.configure(sid,values)
    assert reg.get(sid)["status"]=="UNCONFIGURED"
    reg.close()


def test_activation_persists_across_registry_restart(tmp_path):
    root=tmp_path/"store"
    reg=CapsuleRegistry(root)
    _,sid=provision(reg,1)
    reg.set_active(sid,True)
    reg.close()
    again=CapsuleRegistry(root)
    assert again.get(sid)["desired_active"]==1
    with pytest.raises(ValueError):
        again.configure(sid,credentials(2))
    again.set_active(sid,False)
    again.close()


def test_worker_environment_only_whitelists_parent_system_settings(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    env=build_worker_env(reg,sid,{
        "PATH":"/usr/bin:/bin","HOME":"/tmp/home",
        "UNRELATED_PARENT_VALUE":"not-inherited",
    })
    assert "UNRELATED_PARENT_VALUE" not in env
    assert env["MAX2TG_NO_DOTENV"]=="1"
    assert env["STATE_DIR"]==str(reg.capsule_dir(sid)/"state")
    assert env["MAX2TG_BROWSER_PROFILE"]==str(reg.capsule_dir(sid)/"browser-profile")
    assert env["TG_CHAT_ID"]==credentials(1)["TG_CHAT_ID"]
    reg.close()


def test_second_supervisor_cannot_own_same_registry(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    checkout=Path(__file__).resolve().parents[1]
    sup=CapsuleSupervisor(reg,checkout,worker_module="tests.capsule_dummy_worker")
    try:
        with pytest.raises(RuntimeError):
            CapsuleSupervisor(reg,checkout,worker_module="tests.capsule_dummy_worker")
    finally:
        sup.close();reg.close()


def test_two_real_local_worker_processes_are_isolated(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,a=provision(reg,1)
    _,b=provision(reg,2)
    reg.set_active(a,True);reg.set_active(b,True)
    sup=CapsuleSupervisor(reg,Path(__file__).resolve().parents[1],
                          worker_module="tests.capsule_dummy_worker")
    try:
        sup.tick()
        path_a=reg.capsule_dir(a)/"state"/"inspection.json"
        path_b=reg.capsule_dir(b)/"state"/"inspection.json"
        assert ready(lambda:path_a.is_file() and path_b.is_file())
        x=json.loads(path_a.read_text())
        y=json.loads(path_b.read_text())
        assert x["pid"] != y["pid"]
        for key in ("state_dir","log_dir","profile_dir","tg_chat_id"):
            assert x[key]!=y[key]
        assert x["skip_personal_dotenv"]=="1"
        assert y["skip_personal_dotenv"]=="1"
        assert not x["parent_env_inherited"]
        assert not y["parent_env_inherited"]
        sup.tick()
        assert reg.get(a)["status"]=="ONLINE"
        assert reg.get(b)["status"]=="ONLINE"
        assert lock_held(reg.capsule_dir(a)/"worker.lock")
        assert lock_held(reg.capsule_dir(b)/"worker.lock")
        resource=observe_resources([x["pid"],y["pid"]])
        assert len(resource)==2
        assert all(v["rss_mib"]>0 for v in resource)
        reg.set_active(a,False)
        sup.tick()
        assert reg.get(a)["status"]=="STOPPED"
        assert reg.get(b)["status"]=="ONLINE"
    finally:
        sup.close();reg.close()


def test_worker_crash_backoff_and_restore_private_state(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    reg.set_active(sid,True)
    now=[100.]
    sup=CapsuleSupervisor(reg,Path(__file__).resolve().parents[1],
                          worker_module="tests.capsule_dummy_worker",
                          clock=lambda:now[0])
    try:
        sup.tick()
        path=reg.capsule_dir(sid)/"state"/"inspection.json"
        assert ready(path.is_file)
        previous=sup._children[sid]
        previous.kill()
        previous.wait(timeout=5)
        sup.tick()
        assert reg.get(sid)["status"]=="CRASHED"
        sup.tick()
        assert sid not in sup._children
        now[0]+=8
        sup.tick()
        assert sid in sup._children
        assert sup._children[sid].pid!=previous.pid
        assert ready(lambda:json.loads(path.read_text())["pid"]==sup._children[sid].pid)
        assert reg.get(sid)["restarts"]>=1
    finally:
        sup.close();reg.close()


def test_foreign_worker_lock_blocks_duplicate_process(tmp_path):
    import fcntl
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    reg.set_active(sid,True)
    path=reg.capsule_dir(sid)/"worker.lock"
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    sup=CapsuleSupervisor(reg,Path(__file__).resolve().parents[1],
                          worker_module="tests.capsule_dummy_worker")
    try:
        sup.tick()
        assert sid not in sup._children
        assert reg.get(sid)["last_error"]=="WORKER_ALREADY_RUNNING"
    finally:
        sup.close()
        os.close(fd)
        reg.close()


def test_disabled_capsule_never_starts_without_operator_activation(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    sup=CapsuleSupervisor(reg,Path(__file__).resolve().parents[1],
                          worker_module="tests.capsule_dummy_worker")
    try:
        sup.tick()
        assert not sup._children
    finally:
        sup.close();reg.close()


def test_unreadable_credentials_cannot_activate(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,sid=provision(reg,1)
    private=reg.capsule_dir(sid)/"credentials.json"
    private.chmod(0o644)
    with pytest.raises(PermissionError):
        reg.set_active(sid,True)
    reg.close()


def test_existing_unsafe_root_is_rejected_without_chmod(tmp_path):
    root=tmp_path/"existing"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        CapsuleRegistry(root)
    assert stat.S_IMODE(root.stat().st_mode)==0o755


def test_registry_root_cannot_be_symlink(tmp_path):
    target=tmp_path/"target"
    target.mkdir(mode=0o700)
    alias=tmp_path/"alias"
    alias.symlink_to(target)
    with pytest.raises(ValueError):
        CapsuleRegistry(alias)


def test_inactive_child_keeps_other_tenants_operational(tmp_path):
    reg=CapsuleRegistry(tmp_path/"store")
    _,a=provision(reg,1)
    _,b=provision(reg,2)
    reg.set_active(a,True)
    reg.set_active(b,True)
    sup=CapsuleSupervisor(reg,Path(__file__).resolve().parents[1],
                          worker_module="tests.capsule_dummy_worker")
    try:
        sup.tick()
        assert ready(lambda:(reg.capsule_dir(a)/"state"/"health.json").exists()
                     and (reg.capsule_dir(b)/"state"/"health.json").exists())
        reg.set_active(a,False)
        sup.tick()
        assert a not in sup._children
        assert b in sup._children
        assert sup._children[b].poll() is None
    finally:
        sup.close();reg.close()
