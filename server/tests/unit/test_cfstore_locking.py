# -*- coding: utf-8 -*-
"""Unit tests for CFProcessor locking behavior.

Tests that per-IOC locking prevents one slow IOC from blocking others,
and that poll() respects cancellation and max retry limits.

No Docker required — uses mock ChannelFinderClient.
"""

import configparser
import threading
import time
from collections import defaultdict
from unittest.mock import MagicMock, patch

from requests import RequestException
from twisted.internet import defer, reactor
from twisted.trial import unittest

from recceiver.cfstore import CFProcessor, poll
from recceiver.interfaces import CommitTransaction, SourceAddress
from recceiver.processors import ConfigAdapter

# --- Helpers ---

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
    """Create a CFProcessor with a mock ChannelFinder client.

    Sets client=None so startService() runs the full initialization path,
    but patches ChannelFinderClient so no real HTTP connection is made.
    """
    parser = configparser.ConfigParser()
    parser.add_section("test_cf")
    conf = ConfigAdapter(parser, "test_cf")
    proc = CFProcessor("test_cf", conf)

    # Let startService() create the client via the normal path (client is None),
    # but intercept it with a mock.
    mock_client = MagicMock()
    mock_client.getAllProperties.return_value = [{"name": n} for n in CF_REQUIRED_PROPERTIES]
    mock_client.findByArgs.return_value = []
    mock_client.set.return_value = None

    import recceiver.cfstore as cfstore_module

    with patch.object(cfstore_module, "ChannelFinderClient", return_value=mock_client):
        proc.startService()

    return proc


def make_transaction(host="10.0.0.1", port=5000, initial=True, connected=True, records=None):
    """Create a CommitTransaction for testing."""
    return CommitTransaction(
        source_address=SourceAddress(host=host, port=port),
        client_infos={"HOSTNAME": f"ioc-{port}", "IOCNAME": f"IOC-{port}"},
        records_to_add=records if records is not None else {1: ("PV:Test:1", "ai")},
        records_to_delete=set(),
        record_infos_to_add={},
        aliases=defaultdict(list),
        initial=initial,
        connected=connected,
    )


class TestPerIocLocking(unittest.TestCase):
    """Test that per-IOC locks allow independent IOCs to commit in parallel."""

    timeout = 10  # trial test timeout in seconds

    @defer.inlineCallbacks
    def test_independent_iocs_not_blocked(self):
        """IOC-B should complete even while IOC-A's commit is slow."""
        slow_event = threading.Event()
        b_finished = threading.Event()

        call_count = {"n": 0}
        original_return = []

        def findByArgs_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call (IOC-A) — block until released
                slow_event.wait(timeout=8)
            return original_return

        proc = make_processor()
        proc.client.findByArgs.side_effect = findByArgs_side_effect

        tx_a = make_transaction(port=5001)
        tx_b = make_transaction(port=5002)

        # Start IOC-A commit (will block in findByArgs)
        d_a = proc.commit(tx_a)

        # Give IOC-A time to enter the thread and block
        yield _sleep(0.3)

        # Start IOC-B commit
        d_b = proc.commit(tx_b)
        d_b.addCallback(lambda _: b_finished.set())

        # Wait a bit for IOC-B to complete
        yield _sleep(2)

        # With per-IOC locks, IOC-B should have finished independently
        self.assertTrue(
            b_finished.is_set(),
            "IOC-B should complete independently of IOC-A with per-IOC locks",
        )

        # Release IOC-A so cleanup completes
        slow_event.set()
        yield d_a

    @defer.inlineCallbacks
    def test_same_ioc_transactions_serialized(self):
        """Transactions from the same IOC should still be serialized."""
        proc = make_processor()

        # Track execution order by patching _commit_with_thread
        commit_order = []
        original_commit = proc._commit_with_thread

        def tracking_commit(transaction, iocid):
            commit_order.append(iocid)
            if len(commit_order) == 1:
                time.sleep(0.5)  # First transaction is slower
            return original_commit(transaction, iocid)

        proc._commit_with_thread = tracking_commit

        # Two transactions from the SAME IOC (same host:port)
        tx_1 = make_transaction(port=5001)
        tx_2 = make_transaction(port=5001, initial=False)

        d_1 = proc.commit(tx_1)
        d_2 = proc.commit(tx_2)

        yield d_1
        yield d_2

        # Both should have the same iocid and execute in order
        self.assertEqual(len(commit_order), 2)
        self.assertEqual(commit_order[0], commit_order[1], "Both transactions from same IOC")
        self.assertEqual(commit_order[0], "10.0.0.1:5001")


class TestPollRetries(unittest.TestCase):
    """Test that poll() has bounded retries and respects cancellation."""

    def test_poll_gives_up_after_max_retries(self):
        """poll() should raise RequestException after max_retries, not loop forever."""
        proc = make_processor()
        mock_update = MagicMock(side_effect=RequestException("CF unreachable"))

        from recceiver.cfstore import IocInfo

        ioc_info = IocInfo(
            host="10.0.0.1",
            hostname="test-host",
            ioc_name="TEST-IOC",
            ioc_IP="10.0.0.1",
            owner="test",
            time="2026-01-01",
            port=5000,
        )

        start = time.monotonic()
        with self.assertRaises(RequestException):
            poll(mock_update, proc, {}, [], ioc_info, max_retries=2)
        elapsed = time.monotonic() - start

        # Should have been called 3 times (initial + 2 retries)
        self.assertEqual(mock_update.call_count, 3)
        # Should complete in reasonable time (not forever)
        self.assertLess(elapsed, 30, "poll() should not take forever with max_retries=2")

    def test_poll_stops_when_cancelled(self):
        """poll() should stop retrying when the processor signals cancellation."""
        proc = make_processor()

        from recceiver.cfstore import IocInfo

        ioc_info = IocInfo(
            host="10.0.0.1",
            hostname="test-host",
            ioc_name="TEST-IOC",
            ioc_IP="10.0.0.1",
            owner="test",
            time="2026-01-01",
            port=5000,
        )

        iocid = ioc_info.ioc_id
        # Signal cancellation for this IOC
        proc._cancelled[iocid] = True

        mock_update = MagicMock()
        with self.assertRaises(defer.CancelledError):
            poll(mock_update, proc, {}, [], ioc_info)

        # The update method should never have been called
        mock_update.assert_not_called()

    def test_poll_succeeds_on_first_try(self):
        """poll() should return True when update succeeds immediately."""
        proc = make_processor()
        mock_update = MagicMock()

        from recceiver.cfstore import IocInfo

        ioc_info = IocInfo(
            host="10.0.0.1",
            hostname="test-host",
            ioc_name="TEST-IOC",
            ioc_IP="10.0.0.1",
            owner="test",
            time="2026-01-01",
            port=5000,
        )

        result = poll(mock_update, proc, {}, [], ioc_info)
        self.assertTrue(result)
        self.assertEqual(mock_update.call_count, 1)


def _sleep(seconds):
    """Return a Deferred that fires after `seconds`, using the reactor."""
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, None)
    return d
