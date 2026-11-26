"""One-shot disposable backup rehearsal with no external connections."""
import json,sqlite3,sys
from contextlib import closing
from pathlib import Path
from app.operator_backup import first_checkpoint
from app.coordinated_backup import GATEWAY,WORKER
from tests.supervised_proxy_fixture import assert_sandbox


def logical(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t:sorted(db.execute('SELECT * FROM "'+t.replace('"','""')+'"').fetchall(),key=repr) for t in tables}


def main():
    assert_sandbox();sids=json.loads(sys.argv[1]);assert len(sids)==2 and len(set(sids))==2
    states={sid:Path('/state'+str(i)) for i,sid in enumerate(sids)};registry=Path('/registry')
    result,restored=first_checkpoint(registry,states,snapshots='/snapshots',archive='/archive',keys='/backupkeys',anchor_dir='/anchor',restores='/restores',provisioning_stopped=True)
    files={f'gateway-{n}':registry/n for n in GATEWAY}
    for sid,root in states.items():files.update({f'{sid}-{n}':root/n for n in WORKER})
    for n,path in files.items():
        copy=restored/n
        if n.endswith('.sqlite3'):assert logical(copy)==logical(path)
        else:assert copy.read_bytes()==path.read_bytes()
    assert set(p.name for p in restored.iterdir())==set(files)
    assert all(b'synthetic-secret-' not in p.read_bytes() for p in restored.iterdir())
    assert not any(p.exists() for p in (Path('/secret'),Path('/keys'),Path('/network')))
    result['logical_rows_equal']=True;result['credentials_excluded']=True
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
