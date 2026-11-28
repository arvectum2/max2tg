import asyncio
import os

import pytest

from app.cgroup_retirement import DelegatedRetirement
from app.cohort_guard import CohortGuard
from app.controller_lease import renew
from app.egress_policy import EgressDenied


@pytest.mark.parametrize('mode',['broker_expiry','controller_eof','no_initial','admit_failure'])
async def test_guard_one_retirement(mode):
    c_rd,c_wr=os.pipe()
    b_rd,b_wr=os.pipe()
    calls=[]
    retirement=DelegatedRetirement.__new__(DelegatedRetirement)
    def retire(**kwargs):
        assert kwargs['operator_approved'] and kwargs['verify_owned']()
        calls.append('retire')
        return {'state':'RETIRED'}
    retirement.retire=retire
    retirement.stopped=lambda:calls.count('retire')==1
    guard=CohortGuard(retirement)
    def admit():
        calls.append('admit')
        if mode=='admit_failure':return False
        return True
    try:
        renew(c_wr)
        if mode!='no_initial':renew(b_wr)
        task=asyncio.create_task(guard.monitor(controller_fd=c_rd,broker_fd=b_rd,
            seconds=1,operator_approved=True,verify_owned=lambda:True,admit=admit))
        if mode=='controller_eof':
            await asyncio.sleep(.05)
            os.close(c_wr);c_wr=None
        if mode=='admit_failure':
            with pytest.raises(EgressDenied):await asyncio.wait_for(task,2)
        else:
            result=await asyncio.wait_for(task,2)
            assert result['state']=='RETIRED' and not result['live_launch_authorized']
            if mode=='controller_eof':assert result['reasons']['controller']=='EOF'
            if mode=='no_initial':assert 'admit' not in calls
        assert calls.count('retire')==1
        with pytest.raises(EgressDenied):
            await guard.monitor(controller_fd=c_rd,broker_fd=b_rd,seconds=1,
                operator_approved=True,verify_owned=lambda:True,admit=admit)
    finally:
        if c_wr is not None:os.close(c_wr)
        os.close(b_wr)


async def test_guard_requires_explicit_approval():
    retirement=DelegatedRetirement.__new__(DelegatedRetirement)
    guard=CohortGuard(retirement)
    with pytest.raises(EgressDenied):
        await guard.monitor(controller_fd=10,broker_fd=11,seconds=1,operator_approved=False,
                            verify_owned=lambda:True,admit=lambda:True)


async def test_guard_cancellation_retires_and_keeps_readers_until_stop():
    c_rd,c_wr=os.pipe()
    b_rd,b_wr=os.pipe()
    admitted=asyncio.Event()
    stopped=[]
    retirement=DelegatedRetirement.__new__(DelegatedRetirement)
    def retire(**kwargs):
        assert kwargs['verify_owned']()
        os.fstat(c_rd);os.fstat(b_rd)
        stopped.append(True)
        return {'state':'RETIRED'}
    retirement.retire=retire
    retirement.stopped=lambda:bool(stopped)
    guard=CohortGuard(retirement)
    try:
        renew(c_wr);renew(b_wr)
        def admit():
            admitted.set()
            return True
        task=asyncio.create_task(guard.monitor(controller_fd=c_rd,broker_fd=b_rd,
            seconds=5,operator_approved=True,verify_owned=lambda:True,admit=admit))
        await asyncio.wait_for(admitted.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert stopped==[True]
        for fd in (c_rd,b_rd):
            with pytest.raises(OSError):os.fstat(fd)
    finally:
        os.close(c_wr);os.close(b_wr)


async def test_expiry_during_ownership_verification_never_admits():
    import time
    cr,cw=os.pipe()
    br,bw=os.pipe()
    retired=[]
    r=DelegatedRetirement.__new__(DelegatedRetirement)
    def retire(**kwargs):
        retired.append(True)
        return {'state':'RETIRED'}
    r.retire=retire
    r.stopped=lambda:bool(retired)
    def slow_verify():
        time.sleep(1.05)
        return True
    def forbidden():
        raise AssertionError('Expired grant admitted')
    try:
        renew(cw);renew(bw)
        with pytest.raises(EgressDenied):
            await CohortGuard(r).monitor(controller_fd=cr,broker_fd=br,seconds=1,
                operator_approved=True,verify_owned=slow_verify,admit=forbidden)
        assert retired==[True]
    finally:os.close(cw);os.close(bw)
