import sqlite3
import pytest
from app.recovery_manifest import RecoveryAnchor

def directory(root,name):
    p=root/name;p.mkdir(mode=0o700);return p

def member(p,name,value):
    f=p/name
    with sqlite3.connect(f) as db:db.execute('CREATE TABLE receipts(id INTEGER)');db.execute('INSERT INTO receipts VALUES(?)',(value,))
    f.chmod(0o600)


def test_coordinated_snapshot_and_dirty_gate(tmp_path):
    a=RecoveryAnchor(directory(tmp_path,'anchor'));old=directory(tmp_path,'old');new=directory(tmp_path,'new');names={'gateway.sqlite3','worker.sqlite3'}
    for n in names:member(old,n,1);member(new,n,2)
    with pytest.raises(PermissionError):a.capture(old,names,quiescent=False)
    first=a.capture(old,names,quiescent=True);assert a.verify(old,quiescent=True)['structural_only']
    a.mark_dirty()
    with pytest.raises(PermissionError,match='postdate'):a.verify(old,quiescent=True)
    with pytest.raises(PermissionError,match='reconciliation'):a.capture(new,names,quiescent=True)
    second=a.capture(new,names,quiescent=True,reconciled=True);assert second['epoch']==first['epoch'] and second['generation']==2
    with pytest.raises(PermissionError,match='differs'):a.verify(old,quiescent=True)
    assert a.verify(new,quiescent=True)['members']==2
    with pytest.raises(ValueError,match='Incomplete'):
        (new/'worker.sqlite3').unlink();a.verify(new,quiescent=True)


def test_missing_anchor_and_unsafe_files_fail_closed(tmp_path):
    folder=directory(tmp_path,'snapshot');anchor=directory(tmp_path,'anchor');a=RecoveryAnchor(anchor)
    member(folder,'gateway.sqlite3',1)
    with pytest.raises(FileNotFoundError):a.verify(folder,quiescent=True)
    (folder/'worker.sqlite3').symlink_to(folder/'gateway.sqlite3')
    with pytest.raises(PermissionError):a.capture(folder,{'gateway.sqlite3','worker.sqlite3'},quiescent=True)
    with pytest.raises(PermissionError):a.capture(anchor,{'anchor.json'},quiescent=True)


def test_mixed_generation_and_wal_refused(tmp_path):
    import shutil
    a=RecoveryAnchor(directory(tmp_path,'anchor'));current=directory(tmp_path,'current');mixed=directory(tmp_path,'mixed');names={'gateway.sqlite3','worker.sqlite3'}
    for n in names:member(current,n,2);member(mixed,n,1)
    shutil.copy2(current/'gateway.sqlite3',mixed/'gateway.sqlite3');a.capture(current,names,quiescent=True)
    with pytest.raises(PermissionError,match='differs'):a.verify(mixed,quiescent=True)
    extra=current/'gateway.sqlite3-wal';extra.touch(mode=0o600)
    with pytest.raises(PermissionError,match='membership changed'):a.capture(current,names|{extra.name},quiescent=True)
    fresh=RecoveryAnchor(directory(tmp_path,'fresh-anchor'))
    with pytest.raises(ValueError):fresh.capture(current,names|{extra.name},quiescent=True)
    with pytest.raises(ValueError):a.verify(current,quiescent=True)
