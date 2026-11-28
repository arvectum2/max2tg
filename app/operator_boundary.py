"""Owned child-process shutdown and retirement evidence for trusted operators.

No process discovery, arbitrary PID signalling, command execution or launch
authority. Caller exclusively owns the complete, pre-reviewed cohort, uses
direct non-forking children and keeps independent namespace ownership. A
cooperative stop request is caller-supplied and must be bounded.
"""
import math
import subprocess
import time

from app.egress_policy import EgressDenied
from app.cgroup_retirement import DelegatedRetirement
from app.generation_ledger import require
from app.rotation_fence import _binding


class OwnedCohort:
    """Seal exact direct children, then prove their exit; never adopt by PID."""
    def __init__(self,children):
        require(type(children) is tuple and 1<=len(children)<=66)
        require(all(type(p) is subprocess.Popen and type(p.pid) is int and p.pid>1
                    for p in children))
        require(len({p.pid for p in children})==len(children))
        self._children=children
        self._pids=tuple(p.pid for p in children)
        self._closed=False
        self._attempted=False

    def _check(self):
        require(not self._closed and tuple(p.pid for p in self._children)==self._pids)

    def stopped(self):
        self._check()
        return all(p.poll() is not None for p in self._children)

    def wait_stopped(self,timeout):
        self._check()
        require(type(timeout) in (int,float) and math.isfinite(timeout) and 0<=timeout<=60)
        deadline=time.monotonic()+timeout
        for child in reversed(self._children):
            try:child.wait(timeout=max(0,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:return False
        return self.stopped()

    def stop_once(self,request_stop,*,timeout=10):
        """One cooperative stop request; lack of proof remains blocked."""
        self._check()
        require(not self._attempted)
        self._attempted=True
        require(type(timeout) in (int,float) and math.isfinite(timeout) and 0<=timeout<=60)
        if not self.stopped():
            if request_stop() is not True:return False
        return self.wait_stopped(timeout)

    def close(self):
        self._closed=True


class RetirementBoundary:
    """Fresh read-only proof of a stopped owned cohort and retired namespace."""
    def __init__(self,cohort,namespace):
        require(type(cohort) in (OwnedCohort,DelegatedRetirement))
        self.cohort=cohort
        self.namespace=namespace
        self.binding=_binding(namespace)

    def stopped(self):
        try:
            require(_binding(self.namespace)==self.binding)
            return self.cohort.stopped()
        except (EgressDenied,OSError,ValueError,subprocess.SubprocessError):
            return False

    def retired(self):
        try:
            return (self.stopped() and self.namespace.retired() is True
                    and _binding(self.namespace)==self.binding and self.stopped())
        except (EgressDenied,OSError,ValueError,subprocess.SubprocessError):
            return False
