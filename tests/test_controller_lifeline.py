import asyncio
import os
import subprocess
import sys

import pytest

from app.controller_lifeline import ControllerLifeline
from app.egress_policy import EgressDenied


async def test_owner_exit_closes_lifeline():
    rd,wr=os.pipe()
    p=subprocess.Popen([sys.executable,'-c','import sys,os;sys.stdin.buffer.read();os._exit(17)'],
                       pass_fds=(wr,),stdin=subprocess.PIPE)
    os.close(wr)
    try:
        stop=asyncio.Event()
        with ControllerLifeline(rd,stop) as line:
            assert not stop.is_set()
            p.stdin.close()
            await asyncio.wait_for(stop.wait(),3)
            assert line.reason=='EOF'
        assert p.wait(timeout=3)==17
    finally:
        if not p.stdin.closed:p.stdin.close()
        p.wait(timeout=3)


async def test_preclosed_writer():
    rd,wr=os.pipe();os.close(wr)
    stop=asyncio.Event()
    with ControllerLifeline(rd,stop) as line:
        assert stop.is_set() and line.reason=='EOF'


async def test_unexpected_data_fails_closed():
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with ControllerLifeline(rd,stop) as line:
            os.write(wr,b'x')
            await asyncio.wait_for(stop.wait(),1)
            assert line.reason=='UNEXPECTED_DATA'
    finally:os.close(wr)


async def test_write_end_rejected():
    rd,wr=os.pipe()
    try:
        stop=asyncio.Event()
        with pytest.raises(EgressDenied):ControllerLifeline(wr,stop)
        assert stop.is_set()
    finally:os.close(rd);os.close(wr)
