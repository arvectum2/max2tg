import json
import asyncio
from types import SimpleNamespace
import lz4.block
import msgpack
import pytest
from app.max_wire import decode,encode,HEADER,profile_contact
from app.max_client import MaxClient,OpCode
from app.resolver import ContactResolver

def test_roundtrip_and_large_message_ids():
    packet={"cmd":0,"seq":123,"opcode":64,"payload":{"id":117624820253851896,"text":"Привет"}}
    assert decode(encode(packet))=={"ver":10,**packet}

def test_compressed_frame_and_nested_int64():
    raw=msgpack.packb({"profile":{"contact":{"id":msgpack.ExtType(1,msgpack.packb(436246481))}},"text":"абв"*3000},use_bin_type=True)
    body=lz4.block.compress(raw,store_size=False)
    ratio=(len(raw)+len(body)-1)//len(body)
    frame=HEADER.pack(10,1,4,19,ratio)+len(body).to_bytes(3,"big")+body
    result=decode(frame)
    assert profile_contact(result["payload"])["id"]==436246481

def test_invalid_length_and_size_fail_closed():
    with pytest.raises(ValueError):decode(b"bad")
    with pytest.raises(ValueError):decode(HEADER.pack(10,1,1,19,0)+b"\x00\x00\x0a"+b"x")

async def test_binary_rpc_immediate_reply():
    c=MaxClient("t","d");c._binary_transport=True
    async def send(raw):
        p=decode(raw)
        await c._handle({"cmd":1,"seq":p["seq"],"opcode":p["opcode"],"payload":{"ok":True}})
    c._ws=SimpleNamespace(closed=False,send_bytes=send)
    assert await c.cmd(OpCode.CHAT_GET,{})=={"ok":True}

async def test_nested_profile_and_invalid_ack():
    c=MaxClient("t","d")
    data={"profile":{"contact":{"id":42,"names":[{"firstName":"Me"}]}},"chats":[]}
    await c._handle({"cmd":1,"seq":0,"opcode":19,"payload":data})
    assert c._authorized and c._my_id==42
    r=ContactResolver();r.load_snapshot(data);assert r.my_id==42
    await c._handle({"cmd":1,"seq":0,"opcode":19,"payload":{"profile":{}}})
    assert not c._authorized
