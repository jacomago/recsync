"""Validate schema/recsync.ksy against the golden vectors.

Parses every entry in ``vectors.json`` through the parser compiled from
``schema/recsync.ksy`` and checks the decoded fields. This keeps the Kaitai
schema, SPEC.md, and the canonical Python implementation from drifting apart.

The compiled parser is expected at ``conformance/_generated/recsync.py`` (built
in CI, or locally with ``kaitai-struct-compiler``; see conformance/README.md).
If the parser or the ``kaitaistruct`` runtime is unavailable, the tests skip.
"""

import json
import os
import socket
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GENERATED = os.path.join(_HERE, "_generated")

if _GENERATED not in sys.path:
    sys.path.insert(0, _GENERATED)

try:
    from recsync import Recsync  # noqa: E402  (compiled from schema/recsync.ksy)
except ImportError as exc:  # pragma: no cover - exercised only without the parser
    pytest.skip(
        f"compiled Kaitai parser unavailable ({exc}); "
        "run kaitai-struct-compiler --target python --outdir conformance/_generated "
        "schema/recsync.ksy",
        allow_module_level=True,
    )

with open(os.path.join(_HERE, "vectors.json")) as _handle:
    VECTORS = json.load(_handle)


def _decoded_fields(message, body):
    """Map a parsed Kaitai body to the same logical dict as a vector entry."""
    if message == "ServerGreeting":
        return {"version": body.version}
    if message in ("Ping", "Pong"):
        return {"nonce": body.nonce}
    if message == "ClientGreeting":
        return {
            "version": body.version,
            "client_type": body.client_type,
            "server_key": body.server_key,
        }
    if message == "AddRecord":
        return {
            "record_id": body.recid,
            "kind": body.kind.value,
            "record_type": body.rtype,
            "record_name": body.rname,
        }
    if message == "DelRecord":
        return {"record_id": body.recid}
    if message == "UploadDone":
        return {}
    if message == "AddInfo":
        return {"record_id": body.recid, "key": body.key, "value": body.value}
    raise AssertionError(f"unhandled message {message}")


@pytest.fixture(params=VECTORS, ids=lambda entry: entry["name"])
def vector(request):
    return request.param


def test_kaitai_parses_vector(vector):
    raw = bytes.fromhex(vector["hex"])

    if vector["transport"] == "udp":
        ann = Recsync.Announce.from_bytes(raw)
        decoded = {
            "tcp_port": ann.port,
            "key": ann.server_key,
            "host": socket.inet_ntoa(ann.server_addr),
        }
        assert decoded == vector["fields"]
        return

    frame = Recsync.from_bytes(raw)
    assert frame.header.msg_id.value == vector["msg_id"]
    assert frame.header.body_len == len(raw) - 8
    assert _decoded_fields(vector["message"], frame.body) == vector["fields"]
