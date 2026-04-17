# -*- coding: utf-8 -*-
"""Reproduction test for the 'IOC did not send an initial transaction' production bug.

Root cause:
  A single global DeferredLock serialises ALL IOC commits. When ChannelFinder
  is slow, one IOC's infinite-retry poll() holds the lock indefinitely. Other
  IOC connections time out; their pending initial-commit Deferreds are cancelled
  *before* _commit_with_lock ever runs, so update_ioc_infos never executes and
  the IOC is never added to self.iocs.

Expected behaviour (fix - per-IOC locks):
  Each IOC has its own DeferredLock. IOC A is not blocked by IOC C's slow
  commit. IOC A's _commit_with_lock runs immediately; update_ioc_infos adds
  it to self.iocs before poll() is called. Even if the Deferred is later
  cancelled, the IOC is already registered.
"""

import configparser
import threading
from collections import defaultdict
from unittest.mock import MagicMock, patch

from twisted.internet import defer, reactor
from twisted.trial import unittest

from recceiver.cfstore import CFProcessor
from recceiver.interfaces import CommitTransaction, SourceAddress
from recceiver.processors import ConfigAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CF_REQUIRED_PROPERTIES = [
    "hostName",
    "iocName",
    "pvStatus",
    "time",
    "iocid",
    "iocIP",
    "recceiverID",
    "caPort",
    "pvaPort",
]


def make_processor():
    parser = configparser.ConfigParser()
    parser.add_section("test_cf")
    conf = ConfigAdapter(parser, "test_cf")
    proc = CFProcessor("test_cf", conf)

    mock_client = MagicMock()
    mock_client.getAllProperties.return_value = [{"name": n} for n in CF_REQUIRED_PROPERTIES]
    mock_client.findByArgs.return_value = []
    mock_client.set.return_value = None

    import recceiver.cfstore as cfstore_module

    with patch.object(cfstore_module, "ChannelFinderClient", return_value=mock_client):
        proc.startService()

    return proc


def make_transaction(host="10.0.0.1", port=5000, initial=True, connected=True, records=None):
    return CommitTransaction(
        source_address=SourceAddress(host=host, port=port),
        client_infos={"HOSTNAME": f"ioc-{port}", "IOCNAME": f"IOC-{port}"},
        records_to_add=records if records is not None else {1: (f"PV:{port}:Test", "ai")},
        records_to_delete=set(),
        record_infos_to_add={},
        aliases=defaultdict(list),
        initial=initial,
        connected=connected,
    )


def _sleep(seconds):
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, None)
    return d


# ---------------------------------------------------------------------------
# Reproduction test
# ---------------------------------------------------------------------------


class TestIocNotInListWarning(unittest.TestCase):
    """Reproduce the 'did not send an initial transaction' production warning.

    Scenario
    --------
    IOC-C holds the lock with a slow CF findByArgs call.
    IOC-A queues its initial commit but the connection drops before the lock
    is acquired, so the Deferred is cancelled. The test verifies that
    update_ioc_infos ran for IOC-A's initial commit despite the cancellation —
    i.e. IOC-A is in proc.iocs after the cancel settles.

    With a single global lock this fails: the cancel arrives while IOC-A is
    waiting for the lock, so _commit_with_lock never runs and update_ioc_infos
    is never called.

    With per-IOC locks this passes: IOC-A runs _commit_with_lock on its own
    free lock immediately, update_ioc_infos executes before poll() sees the
    cancellation, and IOC-A is present in proc.iocs.
    """

    timeout = 15

    @defer.inlineCallbacks
    def test_update_ioc_infos_runs_despite_cancellation(self):
        """Cancelling an initial commit must not prevent update_ioc_infos from
        running.

        When a Deferred is cancelled while waiting for a contended lock,
        _commit_with_lock never runs and update_ioc_infos is skipped entirely.
        With per-IOC locks the function runs on an uncontended lock before
        cancellation takes effect, so the IOC is registered in proc.iocs.
        """
        block_event = threading.Event()
        call_count = {"n": 0}

        def controlled_findByArgs(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Block the first findByArgs call (IOC-C's iocid lookup) to
                # simulate slow ChannelFinder and hold the lock.
                block_event.wait(timeout=12)
            return []

        proc = make_processor()
        proc.client.findByArgs.side_effect = controlled_findByArgs

        # IOC-C commits first; its thread blocks inside findByArgs.
        tx_c = make_transaction(host="10.0.0.3", port=5003)
        d_c = proc.commit(tx_c)

        # Give IOC-C's thread time to acquire the lock and enter findByArgs.
        yield _sleep(0.4)

        # IOC-A's initial commit: with a global lock it waits behind IOC-C;
        # with per-IOC locks it runs immediately on its own lock.
        tx_a_initial = make_transaction(host="10.0.0.1", port=5001)
        d_a = proc.commit(tx_a_initial)

        # Suppress the expected CancelledError so trial doesn't fail on it.
        d_a.addErrback(lambda err: err.trap(defer.CancelledError))

        # Simulate IOC-A's TCP connection dropping.
        d_a.cancel()

        # Wait for d_a to settle; with per-IOC locks the thread must finish
        # before the per-IOC lock is released.
        yield d_a

        # With per-IOC locks _commit_with_thread runs update_ioc_infos before
        # poll() checks cancellation, so IOC-A must be present in proc.iocs.
        # With a single global lock _commit_with_lock never ran, so it is absent.
        self.assertIn(
            "10.0.0.1:5001",
            proc.iocs,
            "IOC-A should be in proc.iocs: update_ioc_infos must run before "
            "cancellation takes effect (requires per-IOC locking).",
        )

        # Cleanup: release IOC-C.
        block_event.set()
        yield d_c
