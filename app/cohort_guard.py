"""Acceptance operator: two independent progress leases, one owned cohort.

The external operator must be outside the delegated subtree and own all
writers, descriptors and process placement. No process discovery, startup
or adoption authority. A blocked operator itself remains an external gate.
"""
import asyncio
from contextlib import ExitStack

from app.cgroup_retirement import DelegatedRetirement
from app.controller_lease import ControllerLease
from app.generation_ledger import require


class CohortGuard:
    def __init__(self,retirement):
        require(type(retirement) is DelegatedRetirement)
        self.retirement=retirement
        self.used=False

    async def monitor(self,*,controller_fd,broker_fd,seconds,operator_approved,
                      verify_owned,admit):
        require(not self.used)
        self.used=True
        require(operator_approved is True and controller_fd!=broker_fd)
        stop=asyncio.Event()
        reasons=[]
        failure=None
        with ExitStack() as resources:
            try:
                for role,fd in (('controller',controller_fd),('broker',broker_fd)):
                    lease=resources.enter_context(ControllerLease(fd,stop,seconds=seconds))
                    reasons.append((role,lease))
                # Both initial progress records must be valid before admission.
                await asyncio.gather(*(lease.ready.wait() for _,lease in reasons))
                if not stop.is_set():
                    require(all(lease.valid() for _,lease in reasons))
                    require(verify_owned() is True)
                    require(all(lease.valid() for _,lease in reasons))
                    require(admit() is True)
                    if all(lease.valid() for _,lease in reasons):
                        await stop.wait()
            except BaseException as exc:
                failure=exc
            observed={role:lease.reason for role,lease in reasons if lease.reason}
            # Keep both readers held until the exact owned cohort is retired.
            # Closing a pipe first could cause a still-healthy writer to exit,
            # invalidating the reviewed process inventory before shutdown.
            evidence=self.retirement.retire(operator_approved=True,verify_owned=verify_owned)
            require(evidence['state']=='RETIRED' and self.retirement.stopped())
        if failure is not None:raise failure
        return {'state':'RETIRED','reasons':observed,'automatic_retry':False,
                'live_launch_authorized':False}
