"""Round-trip the language-neutral golden vectors against the Python protocol.

The fixtures in ``conformance/vectors.json`` are the shared conformance suite for
every RecSync implementation. This test proves the committed fixtures still match
the canonical Python encoder/decoder, so a drift in either is caught in CI.
"""

import json
import os

import pytest

from recceiver.protocol import announce, messages

VECTORS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "conformance", "vectors.json")

with open(VECTORS_PATH) as _handle:
    VECTORS = json.load(_handle)


def _build(entry):
    """Reconstruct the message described by a fixture entry."""
    fields = dict(entry["fields"])
    if entry["transport"] == "udp":
        return announce.Announce(**fields)
    cls = getattr(messages, entry["message"])
    if "kind" in fields:
        fields["kind"] = messages.RecordKind(fields["kind"])
    return cls(**fields)


@pytest.fixture(params=VECTORS, ids=lambda entry: entry["name"])
def vector(request):
    return request.param


def test_encode_matches_vector(vector):
    message = _build(vector)
    raw = message.encode() if vector["transport"] == "udp" else message.frame()
    assert raw.hex() == vector["hex"]


def test_decode_matches_vector(vector):
    raw = bytes.fromhex(vector["hex"])
    expected = _build(vector)

    if vector["transport"] == "udp":
        assert announce.Announce.decode(raw) == expected
        return

    header = messages.Header.decode(raw[:8])
    assert header.msg_id == vector["msg_id"]
    assert header.body_length == len(raw) - 8
    cls = getattr(messages, vector["message"])
    assert cls.decode(raw[8:]) == expected
