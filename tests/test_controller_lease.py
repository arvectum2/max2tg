import asyncio
import os
import time

import pytest

from app.controller_lease import ControllerLease, FRAME, renew
from app.egress_policy import EgressDenied


async def test_initial_lease_required_and_expiry_terminal():
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with ControllerLease(rd,stop,seconds=1) as lease:
            assert not lease.valid() and not lease.ready.is_set()
            renew(wr)
            await asyncio.wait_for(lease.ready.wait(),.5)
            assert lease.valid()
            await asyncio.wait_for(stop.wait(),2)
            assert lease.reason=='LEASE_EXPIRED' and not lease.valid()
            renew(wr)
            assert not lease.valid()
    finally:os.close(wr)


@pytest.mark.parametrize('kind',['stale','future','duplicate'])
async def test_invalid_frame_fails_closed(kind):
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with ControllerLease(rd,stop,seconds=1) as lease:
            sent=time.monotonic_ns()
            if kind=='stale':sent-=2_000_000_000
            if kind=='future':sent+=2_000_000_000
            os.write(wr,FRAME.pack(sent)*(2 if kind=='duplicate' else 1))
            await asyncio.wait_for(stop.wait(),.5)
            assert lease.reason=='INVALID_RENEWAL' and not lease.valid()
    finally:os.close(wr)


async def test_partial_frame_never_grants():
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with ControllerLease(rd,stop,seconds=1) as lease:
            os.write(wr,b'x')
            await asyncio.wait_for(stop.wait(),2)
            assert lease.reason=='LEASE_EXPIRED' and not lease.valid()
    finally:os.close(wr)


async def test_eof_before_initial_progress():
    rd,wr=os.pipe();os.close(wr)
    stop=asyncio.Event()
    with ControllerLease(rd,stop,seconds=1) as lease:
        assert lease.ready.is_set() and stop.is_set() and lease.reason=='EOF'


async def test_delayed_frame_uses_sender_deadline():
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with ControllerLease(rd,stop,seconds=1) as lease:
            os.write(wr,FRAME.pack(time.monotonic_ns()-800_000_000))
            await asyncio.wait_for(lease.ready.wait(),.1)
            await asyncio.wait_for(stop.wait(),.5)
            assert lease.reason=='LEASE_EXPIRED'
    finally:os.close(wr)


@pytest.mark.parametrize('seconds',[0,31,True,1.5])
async def test_invalid_duration(seconds):
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with pytest.raises(EgressDenied):ControllerLease(rd,stop,seconds=seconds)
        assert stop.is_set()
    finally:os.close(rd);os.close(wr)
