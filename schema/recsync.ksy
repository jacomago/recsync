meta:
  id: recsync
  title: RecSync protocol
  endian: be
  encoding: UTF-8
doc: |
  Machine-readable definition of the RecSync wire protocol. See ../SPEC.md for
  the normative specification.

  The root type parses a single TCP frame: an 8-byte header followed by a body
  selected by the header's message id. The UDP announce packet is the `announce`
  type, parsed by invoking it directly (Recsync.Announce.from_bytes(...)).

  Kaitai describes structural decoding only; semantic rules (alias requires an
  empty record type, non-empty names/keys, the encode direction) live in SPEC.md
  and the reference Python implementation.
seq:
  - id: header
    type: frame_header
  - id: body
    size: header.body_len
    type:
      switch-on: header.msg_id
      cases:
        'msg_id::server_greet': server_greet
        'msg_id::ping': ping
        'msg_id::client_greet': client_greet
        'msg_id::pong': pong
        'msg_id::add_record': add_record
        'msg_id::del_record': del_record
        'msg_id::upload_done': upload_done
        'msg_id::add_info': add_info
enums:
  msg_id:
    0x8001: server_greet
    0x8002: ping
    0x0001: client_greet
    0x0002: pong
    0x0003: add_record
    0x0004: del_record
    0x0005: upload_done
    0x0006: add_info
  record_kind:
    0: record
    1: alias
types:
  frame_header:
    doc: 8-byte header preceding every TCP message body.
    seq:
      - id: magic
        contents: [0x52, 0x43]
        doc: PROTO_ID, ASCII "RC".
      - id: msg_id
        type: u2
        enum: msg_id
      - id: body_len
        type: u4
        doc: Length in bytes of the body following this header.
  announce:
    doc: |
      UDP packet broadcast by the server to advertise its TCP endpoint.
      Not reachable from the root frame; parse it directly. 16 bytes; any
      trailing bytes are ignored by clients.
    seq:
      - id: magic
        contents: [0x52, 0x43]
        doc: PROTO_ID, ASCII "RC".
      - id: reserved
        type: u2
        doc: Must be 0.
      - id: server_addr
        size: 4
        doc: IPv4 address (big-endian) the client should connect to.
      - id: port
        type: u2
        doc: TCP port the client should connect to.
      - id: pad
        size: 2
        doc: Ignored.
      - id: server_key
        type: u4
        doc: Opaque value the client echoes in its ClientGreeting.
  server_greet:
    seq:
      - id: version
        type: u1
  ping:
    seq:
      - id: nonce
        type: u4
  client_greet:
    seq:
      - id: version
        type: u1
      - id: client_type
        type: u1
      - id: pad
        size: 2
        doc: Reserved; 0.
      - id: server_key
        type: u4
  pong:
    seq:
      - id: nonce
        type: u4
  add_record:
    seq:
      - id: recid
        type: u4
      - id: kind
        type: u1
        enum: record_kind
      - id: rtlen
        type: u1
        doc: Byte length of rtype.
      - id: rnlen
        type: u2
        doc: Byte length of rname.
      - id: rtype
        type: str
        size: rtlen
        doc: Record type (empty for an alias).
      - id: rname
        type: str
        size: rnlen
        doc: Record or alias name.
  del_record:
    seq:
      - id: recid
        type: u4
  upload_done:
    seq:
      - id: dummy
        type: u4
        doc: Accepted but ignored.
  add_info:
    seq:
      - id: recid
        type: u4
        doc: Record id, or 0 for IOC-level info.
      - id: keylen
        type: u1
        doc: Byte length of key.
      - id: pad
        size: 1
        doc: Reserved; 0.
      - id: valen
        type: u2
        doc: Byte length of value.
      - id: key
        type: str
        size: keylen
      - id: value
        type: str
        size: valen
