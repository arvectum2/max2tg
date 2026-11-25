#!/usr/bin/env python3
"""Synthetic cache work comparison; no accounts or network.

Old path's saves are counted but NOT executed, to avoid 100k fsyncs. Both
CPU-only paths use the same ledger and cache implementation. Candidate
real durable rebuild is separately timed, including its one JSON fsync.
"""
import argparse,json,statistics,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.capsule_egress_outbox import CapsuleEgressOutbox
from app.capsule_shared_listener import SharedCapsuleTextListener
from app.topics import TopicStore

SID='a'*24

def old_repair(q,t):
    n=0
    for cid,mid,tid in q.db.execute("SELECT max_chat_id,max_message_id,telegram_id FROM worker_telegram_intents WHERE status='SENT' AND max_chat_id IS NOT NULL"):
        if type(tid) is not int or tid<=0 or t.get_topic(cid) is None:continue
        old=t.tg_for_max_message(cid,mid);other=t.max_for_tg_message(cid,tid)
        if old is not None and old!=tid or other is not None and other!=mid:raise RuntimeError('Conflicting confirmed mapping')
        if old is None:t.set_message(cid,mid,tid);n+=1
    return n

def timed(fn):
    start=time.perf_counter();result=fn();return (time.perf_counter()-start)*1000,result

def run(n):
    with tempfile.TemporaryDirectory(prefix='m2receipt-') as folder:
        root=Path(folder);q=CapsuleEgressOutbox(root/'ledger.sqlite3',SID)
        try:
            q.db.execute('BEGIN IMMEDIATE');q.db.executemany("INSERT INTO worker_telegram_intents(source_key,text,fingerprint,status,telegram_id,updated_at,max_chat_id,max_message_id,submission_attempted) VALUES(?,'synthetic','synthetic','SENT',?,0,'42',?,1)",((f'max:{i}',i+100,str(i)) for i in range(1,n+1)));q.db.execute('COMMIT')
            t=TopicStore(str(root/'old.json'),strict=True);t.set_topic(42,77,'synthetic')
            # Plain counter avoids MagicMock retaining a large call list.
            saves=[0]
            def count_save():saves[0]+=1
            with patch.object(t,'_save',count_save):
                old_repair(q,t);saves[0]=0;old=[]
                for _ in range(3):old.append(timed(lambda:old_repair(q,t))[0])
            old_writes=saves[0]//3
            t=TopicStore(str(root/'new.json'),strict=True);t.set_topic(42,77,'synthetic');a=SharedCapsuleTextListener(None,t,None,q,NS(sid=SID))
            with patch.object(t,'_save',wraps=t._save) as save:
                cold,repaired=timed(a.repair_receipts);assert repaired==min(n,1000) and save.call_count==1
                warm=[timed(a.repair_receipts)[0] for _ in range(100)];assert save.call_count==1
                # Late confirmation on an old pending intent is indexed at ACK.
                q.stage('late','late',max_chat_id=42,max_message_id='late');q.db.execute("UPDATE worker_telegram_intents SET status='SENT',telegram_id=? WHERE source_key='late'",(n+1000,));incremental,num=timed(a.repair_receipts);assert num==1 and save.call_count==2
            assert q.counts()=={'SENT':n+1} and t.tg_for_max_message(42,'late')==n+1000
            return {'history':n,'old_warm_cpu_only_ms_median':round(statistics.median(old),4),'old_warm_save_calls':old_writes,'new_cold_durable_ms':round(cold,4),'new_warm_ms_median':round(statistics.median(warm),4),'new_warm_save_calls':0,'new_one_receipt_durable_ms':round(incremental,4),'new_cold_rows':repaired}
        finally:q.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sizes',nargs='+',type=int,choices=(1000,10000,100000),default=[1000,10000,100000]);a=p.parse_args()
    for n in a.sizes:print(json.dumps(run(n),sort_keys=True),flush=True)
