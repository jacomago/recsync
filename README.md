RecSync
=======

RecSync is a protocol for synchronising the list of [EPICS] process-variable
records provided by IOCs with one or more central servers. A **RecCaster**
client, running inside an EPICS IOC, uploads the IOC's records and metadata to a
**RecCeiver** server, which keeps a complete, live view of every connected IOC's
records (for example to feed [ChannelFinder]).

This repository is the home of the **protocol specification**.

- **[SPEC.md](SPEC.md)** — the normative wire-protocol specification: transport,
  connection lifecycle, message framing, and every message format.
- **[conformance/](conformance/)** — language-neutral golden test vectors that
  any implementation can validate against.

Implementations
---------------

- **RecCeiver** (Python server) — in [`server/`](server/) (being moved to its
  own repository).
- **RecCaster** — the EPICS IOC client, in
  [ChannelFinder/reccaster](https://github.com/ChannelFinder/reccaster).
- **recsync-rs** — an independent Rust implementation, in
  [ChannelFinder/recsync-rs](https://github.com/ChannelFinder/recsync-rs).

[EPICS]: https://epics-controls.org/
[ChannelFinder]: https://github.com/ChannelFinder/ChannelFinder-directory
