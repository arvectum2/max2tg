"""The outside watchdog must not misinterpret old ONLINE status."""
import runpy
from pathlib import Path
evaluate=runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/max2tg-watchdog.py"))["evaluate"]

def test_old_health_file_from_previous_process_is_not_online():
    assert evaluate(44,{"pid":33},10,"STARTING",200)==("HEARTBEAT_STALE",True)

def test_frozen_process_is_restartable():
    assert evaluate(44,{"pid":44},200,"STARTING",500)==("HEARTBEAT_STALE",True)

def test_wait_for_delayed_mount_and_proxy_without_restart_loop():
    assert evaluate(44,{},1000,"WAIT_REPO",1)==("WAIT_REPO",False)
    assert evaluate(44,{},1000,"WAIT_PROXY",1)==("WAIT_PROXY",False)

def test_fresh_worker_and_broken_runtime():
    assert evaluate(44,{"pid":44},20,"STARTING",200)==("RUNNING",False)
    assert evaluate(44,{},1000,"RUNTIME_INVALID",0)==("RUNTIME_INVALID",False)
    assert evaluate(None,{},1000,"STARTING",300)==("WORKER_MISSING",False)
