import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.egress_policy import EgressDenied
from app.operator_boundary import OwnedCohort, RetirementBoundary


def child():
    return subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],
                            stdin=subprocess.PIPE)


def finish(p):
    if p.stdin and not p.stdin.closed:p.stdin.close()
    p.wait(timeout=5)


def test_exact_cohort_cooperative_stop():
    a,b=child(),child()
    try:
        cohort=OwnedCohort((a,b))
        assert not cohort.stopped()
        assert not cohort.wait_stopped(.02)
        def request():
            a.stdin.close();b.stdin.close()
            return True
        assert cohort.stop_once(request,timeout=5)
        assert cohort.stopped()
        assert a.returncode==b.returncode==0
        with pytest.raises(EgressDenied):cohort.stop_once(request)
        cohort.close()
        with pytest.raises(EgressDenied):cohort.stopped()
    finally:
        finish(a);finish(b)


def test_incomplete_stop_never_claims_success():
    a,b=child(),child()
    try:
        cohort=OwnedCohort((a,b))
        def request():
            a.stdin.close()
            return True
        assert not cohort.stop_once(request,timeout=.05)
        assert not cohort.stopped()
        with pytest.raises(EgressDenied):cohort.stop_once(request)
        finish(b)
        assert cohort.wait_stopped(5)
    finally:finish(a);finish(b)


def test_retirement_requires_both_observations():
    p=child()
    try:
        c=OwnedCohort((p,))
        ns=SimpleNamespace(check=lambda:None,binding=lambda:{'namespace':[1,2]},retired=lambda:True)
        b=RetirementBoundary(c,ns)
        assert not b.retired()
        finish(p)
        assert b.stopped() and b.retired()
        ns.retired=lambda:False
        assert b.stopped() and not b.retired()
        ns.binding=lambda:{'namespace':[1,3]}
        assert not b.stopped() and not b.retired()
    finally:finish(p)


def test_bad_inventory_rejected():
    p=child()
    try:
        for bad in ((),(p,p),[p],(p.pid,)):
            with pytest.raises(EgressDenied):OwnedCohort(bad)
    finally:finish(p)


@pytest.mark.parametrize('timeout',[-1,float('nan'),61,True])
def test_timeout_bounds(timeout):
    p=child()
    try:
        c=OwnedCohort((p,))
        with pytest.raises(EgressDenied):c.wait_stopped(timeout)
    finally:finish(p)
