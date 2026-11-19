"""MAX web transport v10: MessagePack payload, optionally raw LZ4 block."""
from __future__ import annotations

import struct
import lz4.block
import msgpack

HEADER = struct.Struct(">BBhhB")
MAX_FRAME = 16 * 1024 * 1024
MAX_DECOMPRESSED = 32 * 1024 * 1024

def _ext(code, data):
    if code == 1:
        value = msgpack.unpackb(data, raw=False, strict_map_key=False)
        if type(value) is not int:
            raise ValueError("invalid MAX int64 extension")
        return value
    return msgpack.ExtType(code, data)

def encode(packet):
    body = msgpack.packb(packet["payload"], use_bin_type=True)
    if len(body) >= MAX_FRAME:
        raise ValueError("MAX frame too large")
    return (HEADER.pack(10, packet["cmd"], packet["seq"], packet["opcode"], 0)
            + len(body).to_bytes(3, "big") + body)

def decode(frame):
    if len(frame) < 10:
        raise ValueError("MAX frame header truncated")
    version, command, seq, opcode, ratio = HEADER.unpack_from(frame)
    size = int.from_bytes(frame[7:10], "big")
    body = frame[10:]
    if version != 10 or size != len(body):
        raise ValueError("MAX frame version or length mismatch")
    if ratio:
        bound = size * ratio
        if bound > MAX_DECOMPRESSED:
            raise ValueError("MAX decompression limit exceeded")
        body = lz4.block.decompress(body, uncompressed_size=bound)
    payload = msgpack.unpackb(body, raw=False, strict_map_key=False,
                              ext_hook=_ext, timestamp=2) if body else {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("MAX payload is not a map")
    return {"ver": version, "cmd": command, "seq": seq,
            "opcode": opcode, "payload": payload}

def profile_contact(snapshot):
    """Current snapshot wraps the contact; older snapshots were flat."""
    profile = snapshot.get("profile")
    if not isinstance(profile, dict):
        return {}
    contact = profile.get("contact", profile)
    return contact if isinstance(contact, dict) else {}
