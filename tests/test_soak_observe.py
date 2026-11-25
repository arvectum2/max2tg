from collections import Counter
import pytest
from tests.synthetic_mixed_load import schedule,summarize


def test_three_cycles_unique_receipts_and_idle_gates():
    waves=schedule(16,3);counts=Counter(i for _,pairs in waves for i,j in pairs)
    assert len(waves)==36 and waves[-1][0]==250 and counts[0]==57 and counts[1]==33
    assert len({(i,j) for _,pairs in waves for i,j in pairs})==552
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in waves for i,j in pairs for d in ('in','out')]
    checks={str(i):[35,125,215] for i in range(16)};samples=[{'at':at,'pending_in':0,'pending_out':0,'ready':16} for at in (35,45,125,135,215,225)]
    assert summarize(16,effects,checks,samples,cycles=3)['messages']==1104
    with pytest.raises(ValueError):summarize(16,effects,checks,samples[:-2],cycles=3)
    with pytest.raises(ValueError):summarize(16,effects,{str(i):[35,125] for i in range(16)},samples,cycles=3)
    with pytest.raises(ValueError):schedule(16,4)


def test_resource_checkpoints_compare_idle_not_transient():
    from tests.soak_observe import summarize_resources
    def c(at,fds,db):return {'at':at,'roles':{'gateway':dict(fds=fds,sockets=3,threads=1,rss_kib=40000,db_bytes=db,db_files=3,db_main_bytes=db,wal_bytes=0,shm_bytes=0)}}
    points=[c(0,20,100),c(35,10,200),c(125,11,300),c(215,10,400),c(255,20,500)]
    result=summarize_resources(points,cycles=3);assert result['max_idle_fd_growth_per_role']==0 and result['checkpoints'][-1]['db_bytes']==500
    with pytest.raises(ValueError):summarize_resources(points[:-1],cycles=3)
    with pytest.raises(ValueError):summarize_resources([*points[:3],c(215,19,400),points[-1]],cycles=3)
