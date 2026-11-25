import asyncio
import pytest
from tests.synthetic_long_poll import UpdateWakeup

@pytest.mark.asyncio
async def test_long_poll_wakes_on_matching_update_and_preserves_offset():
    p=UpdateWakeup();rows=[1];select=lambda:[x for x in rows if x>=2]
    task=asyncio.create_task(p.get(select,2));await asyncio.sleep(0);assert not task.done()
    p.notify();await asyncio.sleep(.01);assert not task.done()
    rows.append(2);p.notify();assert await asyncio.wait_for(task,.5)==[2]
    assert await p.get(lambda:rows,2)==[1,2]
    assert await p.get(lambda:[],0)==[]

@pytest.mark.asyncio
async def test_long_poll_timeout_cancel_and_reuse():
    p=UpdateWakeup();loop=asyncio.get_running_loop();at=loop.time();assert await p.get(lambda:[],1)==[];assert loop.time()-at>=.95
    t=asyncio.create_task(p.get(lambda:[],2));await asyncio.sleep(0);t.cancel()
    with pytest.raises(asyncio.CancelledError):await t
    assert await p.get(lambda:[3],1)==[3]
    with pytest.raises(ValueError):await p.get(lambda:[],31)
