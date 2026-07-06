# RecSync conformance vectors

`vectors.json` is a language-neutral set of golden encodings for the
[RecSync protocol](../SPEC.md). It is the shared conformance suite for every
implementation: the Python RecCeiver server, the Rust `recsync-rs` `wire` crate,
and any future port should all validate against this one file.

## Fixture format

`vectors.json` is a JSON array. Each entry is one message:

```json
{
  "name": "add_record",
  "transport": "tcp",
  "message": "AddRecord",
  "msg_id": 3,
  "fields": { "record_id": 11, "kind": 0, "record_type": "ai", "record_name": "IOC1:PV1" },
  "hex": "52430003000000120000000b000200086169494f43313a505631"
}
```

| Key | Meaning |
|-----|---------|
| `name` | Unique, human-readable id for the case. |
| `transport` | `"tcp"` (framed message) or `"udp"` (announce packet). |
| `message` | Logical message name (matches the [spec](../SPEC.md) registry). |
| `msg_id` | TCP message id (omitted for `udp` entries). |
| `fields` | The logical field values. `kind` is the numeric `KIND` discriminant. |
| `hex` | The exact bytes. For `tcp` this is the **full frame** (8-byte header + body); for `udp` it is the 16-byte announce packet. |

## How to use the vectors

An implementation **conforms** if, for every entry:

- **Encode:** building the message from `fields` and serialising it produces
  exactly `hex` (header included for `tcp`).
- **Decode:** parsing `hex` reproduces the `fields`.

The Python reference check lives in
[`server/tests/test_vectors.py`](../server/tests/test_vectors.py) and runs under
the normal test suite.

## Regenerating

`vectors.json` is generated from the canonical Python implementation. After any
intentional protocol change, regenerate it from the repository root:

```bash
PYTHONPATH=server python conformance/generate_vectors.py
```

The committed `vectors.json` MUST be identical to the script's output, so a
regeneration that produces a diff means the fixtures and the implementation have
drifted.

## Validating the Kaitai schema

[`schema/recsync.ksy`](../schema/recsync.ksy) is checked against these vectors by
[`test_kaitai.py`](test_kaitai.py): it parses each vector's bytes through the
parser compiled from the schema and asserts the decoded fields match. The
`conformance` GitHub workflow runs this on every change to `schema/`,
`conformance/`, or the protocol implementation.

To run it locally you need the [Kaitai Struct compiler] and the Python runtime:

```bash
# compiler: download the release zip (needs Java), or `brew install kaitai-struct-compiler`
pip install kaitaistruct
kaitai-struct-compiler --target python --outdir conformance/_generated schema/recsync.ksy
python -m pytest conformance/test_kaitai.py -v
```

The generated parser lands in `conformance/_generated/` (git-ignored); the test
skips if it has not been compiled.

[Kaitai Struct compiler]: https://kaitai.io/#download
