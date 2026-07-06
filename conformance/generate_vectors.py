#!/usr/bin/env python3
"""Regenerate conformance/vectors.json from the canonical Python implementation.

The Python implementation in ``recceiver.protocol`` is the single source of
truth for the RecSync wire protocol. This script encodes a representative
instance of every message (and the UDP announce packet) and records the exact
bytes, producing a language-neutral fixture that any implementation can validate
against (see conformance/README.md).

Run from the repository root with the server package importable, e.g.::

    PYTHONPATH=server python conformance/generate_vectors.py

The committed vectors.json must be identical to this script's output.
"""

import json
import os
import sys

# Allow running from the repo root without installing the server package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(os.path.dirname(_HERE), "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from recceiver.protocol import announce, messages  # noqa: E402

OUTPUT = os.path.join(_HERE, "vectors.json")


def tcp(name, message):
    """Build a fixture entry for a framed TCP message."""
    return {
        "name": name,
        "transport": "tcp",
        "message": type(message).__name__,
        "msg_id": message.msg_id,
        "fields": _fields(message),
        "hex": message.frame().hex(),
    }


def udp(name, packet):
    """Build a fixture entry for a UDP announce packet."""
    return {
        "name": name,
        "transport": "udp",
        "message": type(packet).__name__,
        "fields": {"tcp_port": packet.tcp_port, "key": packet.key, "host": packet.host},
        "hex": packet.encode().hex(),
    }


def _fields(message):
    """Logical fields of a message, with enums flattened to their int value."""
    out = {}
    for key, value in vars(message).items():
        out[key] = int(value) if isinstance(value, messages.RecordKind) else value
    return out


def build_vectors():
    return [
        tcp("server_greeting", messages.ServerGreeting(version=0)),
        tcp("ping", messages.Ping(nonce=0x12345678)),
        tcp(
            "client_greeting",
            messages.ClientGreeting(version=3, client_type=0, server_key=0xCAFEF00D),
        ),
        tcp("pong", messages.Pong(nonce=0x12345678)),
        tcp(
            "add_record",
            messages.AddRecord(
                record_id=11,
                kind=messages.RecordKind.RECORD,
                record_type="ai",
                record_name="IOC1:PV1",
            ),
        ),
        tcp(
            "add_record_alias",
            messages.AddRecord(
                record_id=11,
                kind=messages.RecordKind.ALIAS,
                record_type="",
                record_name="IOC1:PV1:ALIAS",
            ),
        ),
        tcp("del_record", messages.DelRecord(record_id=11)),
        tcp("upload_done", messages.UploadDone()),
        tcp(
            "add_info_record",
            messages.AddInfo(record_id=11, key="alarm", value="MAJOR"),
        ),
        tcp(
            "add_info_ioc",
            messages.AddInfo(record_id=0, key="iocName", value="IOC-1"),
        ),
        udp(
            "announce",
            announce.Announce(tcp_port=1234, key=0xCAFEF00D, host="127.0.0.1"),
        ),
    ]


def main():
    vectors = build_vectors()
    with open(OUTPUT, "w") as handle:
        json.dump(vectors, handle, indent=2)
        handle.write("\n")
    print(f"wrote {len(vectors)} vectors to {OUTPUT}")


if __name__ == "__main__":
    main()
