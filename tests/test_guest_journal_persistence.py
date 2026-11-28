import pytest

from app.egress_policy import EgressDenied
from tests.guest_journal_persistence import prepare, recover, rows, BEFORE, AFTER


async def test_actual_abrupt_process_restart_no_replay(tmp_path):
    root=tmp_path/'max2tg-journal-test'
    prepare(root)
    assert rows(root)==BEFORE
    with pytest.raises(EgressDenied):await recover(root,require_reboot=True)
    assert not (root/'recovery.json').exists()
    result=await recover(root,require_reboot=False)
    assert result=={'state':'VERIFIED','boot_changed':False,'effects':3,
                   'duplicates':0,'live_launch_authorized':False}
    assert rows(root)==AFTER
    with pytest.raises(EgressDenied):await recover(root,require_reboot=False)
