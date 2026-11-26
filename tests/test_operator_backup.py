import pytest
from app.operator_backup import first_checkpoint


def test_operator_backup_refuses_nonempty_or_overlapping_roots(tmp_path):
    folders={n:tmp_path/n for n in ('registry','state','snapshots','archive','keys','anchor_dir','restores')}
    for p in folders.values():p.mkdir(mode=0o700)
    options={n:folders[n] for n in ('snapshots','archive','keys','anchor_dir','restores')}
    args=(folders['registry'],{'a'*24:folders['state']})
    with pytest.raises(PermissionError):first_checkpoint(*args,**options)
    with pytest.raises(PermissionError):first_checkpoint(*args,**dict(options,archive=folders['state']),provisioning_stopped=True)
    (folders['archive']/'existing').touch()
    with pytest.raises(PermissionError):first_checkpoint(*args,**options,provisioning_stopped=True)
    assert list(folders['archive'].iterdir())==[folders['archive']/'existing'] and not list(folders['snapshots'].iterdir())
