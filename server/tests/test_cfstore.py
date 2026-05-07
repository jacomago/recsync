import threading
import time
from collections import defaultdict
from configparser import ConfigParser
from unittest.mock import MagicMock, patch

from requests import RequestException
from twisted.internet import defer, reactor
from twisted.trial import unittest as trial_unittest

from recceiver.cfstore import CFConfig, CFProcessor, CFPropertyName, IocInfo, push_to_cf
from recceiver.interfaces import CommitTransaction, SourceAddress
from recceiver.processors import ConfigAdapter

# ---------------------------------------------------------------------------
# Helpers — unstarted processor and basic fixtures
# ---------------------------------------------------------------------------


def make_adapter(section: str = "cf", values: dict = None, env: dict = None) -> ConfigAdapter:
    parser = ConfigParser()
    parser.add_section(section)
    for key, value in (values or {}).items():
        parser.set(section, key, str(value))
    adapter = ConfigAdapter(parser, section)
    if env:
        adapter.env_vars = env
    return adapter


def make_processor() -> CFProcessor:
    return CFProcessor("test", make_adapter())


def make_ioc(channelcount: int = 1) -> IocInfo:
    return IocInfo(
        host="1.2.3.4",
        hostname="ioc1.example.com",
        ioc_name="IOC1",
        ioc_IP="1.2.3.4",
        owner="engineer",
        time="2026-01-01T00:00:00",
        port=5064,
        channelcount=channelcount,
    )


# ---------------------------------------------------------------------------
# Helpers — started processor and transaction helpers (for async tests)
# ---------------------------------------------------------------------------


def make_started_processor() -> CFProcessor:
    """Return a started CFProcessor backed by a mock ChannelFinder client."""
    proc = CFProcessor("test_cf", make_adapter("test_cf"))
    mock_client = MagicMock()
    mock_client.getAllProperties.return_value = [{"name": p.value} for p in CFPropertyName]
    mock_client.findByArgs.return_value = []
    mock_client.set.return_value = None
    import recceiver.cfstore as _mod

    with patch.object(_mod, "ChannelFinderClient", return_value=mock_client):
        proc.startService()
    return proc


def make_transaction(
    host: str = "10.0.0.1",
    port: int = 5000,
    initial: bool = True,
    connected: bool = True,
    records=None,
) -> CommitTransaction:
    """Return a CommitTransaction for the given host/port.

    PV names are port-scoped so that different IOCs do not share channels,
    which would trigger ownership-merge logic unrelated to the test.
    """
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


def make_ioc_info(host: str = "10.0.0.1", port: int = 5000, ioc_name: str = "TEST-IOC") -> IocInfo:
    return IocInfo(
        host=host,
        hostname="test-host",
        ioc_name=ioc_name,
        ioc_IP=host,
        owner="test",
        time="2026-01-01",
        port=port,
    )


def _sleep(seconds: float) -> defer.Deferred:
    """Return a Deferred that fires after *seconds* via the reactor."""
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, None)
    return d


# ---------------------------------------------------------------------------
# Synchronous tests — plain pytest style
# ---------------------------------------------------------------------------


class TestCFConfigLoads(trial_unittest.TestCase):
    def test_loads_defaults_without_error(self):
        adapter = make_adapter()
        config = CFConfig.loads(adapter)
        assert isinstance(config, CFConfig)

    def test_default_push_max_retries(self):
        adapter = make_adapter()
        config = CFConfig.loads(adapter)
        assert config.push_max_retries == 10

    def test_push_max_retries_from_config(self):
        adapter = make_adapter(values={"pushmaxretries": "3"})
        config = CFConfig.loads(adapter)
        assert config.push_max_retries == 3

    def test_push_max_retries_from_env(self):
        adapter = make_adapter(env={"pushmaxretries": "7"})
        config = CFConfig.loads(adapter)
        assert config.push_max_retries == 7

    def test_default_push_always_retry(self):
        adapter = make_adapter()
        config = CFConfig.loads(adapter)
        assert config.push_always_retry is True

    def test_alias_disabled_by_default(self):
        adapter = make_adapter()
        config = CFConfig.loads(adapter)
        assert config.alias_enabled is False

    def test_alias_enabled_from_config(self):
        adapter = make_adapter(values={"alias": "true"})
        config = CFConfig.loads(adapter)
        assert config.alias_enabled is True


class TestRemoveChannel(trial_unittest.TestCase):
    def test_missing_iocid_does_not_raise(self):
        proc = make_processor()
        iocid = "1.2.3.4:5064"
        proc.channel_ioc_ids["CHAN:1"].append(iocid)
        # iocid deliberately absent from proc.iocs
        proc.remove_channel("CHAN:1", iocid)
        assert "CHAN:1" not in proc.channel_ioc_ids

    def test_missing_iocid_preserves_channel_when_other_iocs_remain(self):
        proc = make_processor()
        iocid = "1.2.3.4:5064"
        proc.channel_ioc_ids["CHAN:1"].append(iocid)
        proc.channel_ioc_ids["CHAN:1"].append("9.9.9.9:5064")
        proc.remove_channel("CHAN:1", iocid)
        assert "CHAN:1" in proc.channel_ioc_ids
        assert "9.9.9.9:5064" in proc.channel_ioc_ids["CHAN:1"]

    def test_removes_ioc_when_channelcount_reaches_zero(self):
        proc = make_processor()
        ioc = make_ioc(channelcount=1)
        iocid = ioc.ioc_id
        proc.iocs[iocid] = ioc
        proc.channel_ioc_ids["CHAN:1"].append(iocid)
        proc.remove_channel("CHAN:1", iocid)
        assert iocid not in proc.iocs
        assert "CHAN:1" not in proc.channel_ioc_ids

    def test_keeps_ioc_when_channelcount_still_positive(self):
        proc = make_processor()
        ioc = make_ioc(channelcount=2)
        iocid = ioc.ioc_id
        proc.iocs[iocid] = ioc
        proc.channel_ioc_ids["CHAN:1"].append(iocid)
        proc.channel_ioc_ids["CHAN:2"].append(iocid)
        proc.remove_channel("CHAN:1", iocid)
        assert iocid in proc.iocs
        assert proc.iocs[iocid].channelcount == 1


# ---------------------------------------------------------------------------
# Async tests — twisted.trial style (reactor lifecycle managed by trial)
# ---------------------------------------------------------------------------


class TestPerIocLocking(trial_unittest.TestCase):
    """Per-IOC locks allow independent IOCs to commit in parallel."""

    timeout = 10

    @defer.inlineCallbacks
    def test_independent_iocs_not_blocked(self):
        """IOC-B should complete even while IOC-A's commit is slow."""
        slow_event = threading.Event()
        b_finished = threading.Event()

        call_count = {"n": 0}

        def findByArgs_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                slow_event.wait(timeout=8)
            return []

        proc = make_started_processor()
        proc.client.findByArgs.side_effect = findByArgs_side_effect

        tx_a = make_transaction(port=5001)
        tx_b = make_transaction(port=5002)

        d_a = proc.commit(tx_a)

        yield _sleep(0.3)

        d_b = proc.commit(tx_b)
        d_b.addCallback(lambda _: b_finished.set())

        yield _sleep(2)

        self.assertTrue(
            b_finished.is_set(),
            "IOC-B should complete independently of IOC-A with per-IOC locks",
        )

        slow_event.set()
        yield d_a
        yield d_b

    @defer.inlineCallbacks
    def test_same_ioc_transactions_serialized(self):
        """Transactions from the same IOC should still be serialized."""
        proc = make_started_processor()

        commit_order = []
        original_commit = proc._commit_with_thread

        def tracking_commit(transaction, iocid):
            commit_order.append(iocid)
            if len(commit_order) == 1:
                time.sleep(0.5)
            return original_commit(transaction, iocid)

        proc._commit_with_thread = tracking_commit

        tx_1 = make_transaction(port=5001)
        tx_2 = make_transaction(port=5001, initial=False)

        yield proc.commit(tx_1)
        yield proc.commit(tx_2)

        self.assertEqual(len(commit_order), 2)
        self.assertEqual(commit_order[0], commit_order[1], "Both transactions from same IOC")
        self.assertEqual(commit_order[0], "10.0.0.1:5001")


class TestPushToCfRetries(trial_unittest.TestCase):
    """push_to_cf() has bounded retries and propagates cancellation."""

    def test_push_to_cf_gives_up_after_max_retries(self):
        """push_to_cf() returns False after push_max_retries failed attempts."""
        proc = make_started_processor()
        proc.cf_config.push_max_retries = 2
        proc.cf_config.push_always_retry = False
        mock_update = MagicMock(side_effect=RequestException("CF unreachable"))
        ioc_info = make_ioc_info()

        with patch("recceiver.cfstore.time.sleep"):
            result = push_to_cf(mock_update, proc, {}, [], ioc_info)

        self.assertFalse(result)
        self.assertEqual(mock_update.call_count, 2)

    def test_push_to_cf_propagates_cancellation(self):
        """push_to_cf() propagates CancelledError raised by the update method."""
        proc = make_started_processor()
        proc.cf_config.push_max_retries = 5
        proc.cf_config.push_always_retry = False
        mock_update = MagicMock(side_effect=defer.CancelledError("IOC cancelled"))
        ioc_info = make_ioc_info()

        self.assertRaises(defer.CancelledError, push_to_cf, mock_update, proc, {}, [], ioc_info)
        mock_update.assert_called_once()

    def test_push_to_cf_succeeds_on_first_try(self):
        """push_to_cf() returns True when the update succeeds immediately."""
        proc = make_started_processor()
        mock_update = MagicMock()
        ioc_info = make_ioc_info()

        result = push_to_cf(mock_update, proc, {}, [], ioc_info)

        self.assertTrue(result)
        self.assertEqual(mock_update.call_count, 1)


class TestIocNotInListWarning(trial_unittest.TestCase):
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
        """Cancelling an initial commit must not prevent update_ioc_infos from running."""
        block_event = threading.Event()
        call_count = {"n": 0}

        def controlled_findByArgs(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Block the first findByArgs call (IOC-C's iocid lookup) to
                # simulate slow ChannelFinder and hold the lock.
                block_event.wait(timeout=12)
            return []

        proc = make_started_processor()
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
