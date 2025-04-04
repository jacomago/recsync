# -*- coding: utf-8 -*-

import collections
import logging
import random
import struct
import sys
import time

from zope.interface import implementer

from twisted.internet import defer, protocol
from twisted.protocols import stateful

from .interfaces import ITransaction

_log = logging.getLogger(__name__)

_M = 0x5243

_Head = struct.Struct(">HHI")
assert _Head.size == 8

_ping = struct.Struct(">I")
assert _ping.size == 4

_s_greet = struct.Struct(">B")
assert _s_greet.size == 1

_c_greet = struct.Struct(">BBxxI")
assert _c_greet.size == 8

_c_info = struct.Struct(">IBxH")
assert _c_info.size == 8

_c_rec = struct.Struct(">IBBH")
assert _c_rec.size == 8


class CastReceiver(stateful.StatefulProtocol):
    timeout = 3.0
    version = 0

    def __init__(self, active=True):
        from twisted.internet import reactor

        self.reactor = reactor

        self.sess, self.active = None, active
        self.uploadSize, self.uploadStart = 0, 0

        self.rxfn = collections.defaultdict(self.dfact)

        self.rxfn[1] = (self.recvClientGreeting, _c_greet.size)
        self.rxfn[2] = (self.recvPong, _ping.size)
        self.rxfn[3] = (self.recvAddRec, _c_rec.size)
        self.rxfn[4] = (self.recvDelRec, _ping.size)
        self.rxfn[5] = (self.recvDone, -1)
        self.rxfn[6] = (self.recvInfo, _c_info.size)

    def writeMsg(self, msgid, body):
        head = _Head.pack(_M, msgid, len(body))
        msg = b"".join((head, body))
        self.transport.write(msg)

    def dataReceived(self, data):
        self.uploadSize += len(data)
        stateful.StatefulProtocol.dataReceived(self, data)

    def connectionMade(self):
        if self.active:
            # Full speed ahead
            self.phase = 1  # 1: send ping, 2: receive pong
            self.T = self.reactor.callLater(self.timeout, self.writePing)
            self.writeMsg(0x8001, _s_greet.pack(self.version))
            self.uploadStart = time.time()
        else:
            # apply brakes
            self.transport.pauseProducing()

    def connectionLost(self, reason=protocol.connectionDone):
        self.factory.isDone(self, self.active)
        if self.T and self.T.active():
            self.T.cancel()
        del self.T
        if self.sess:
            self.sess.close()
        del self.sess

    def restartPingTimer(self):
        T, self.T = self.T, self.reactor.callLater(self.timeout, self.writePing)
        if T and T.active():
            T.cancel()

    def writePing(self):
        if self.phase == 2:
            self.transport.loseConnection()
            _log.debug("pong missed: close connection")
        else:
            self.restartPingTimer()
            self.phase = 2
            self.nonce = random.randint(0, 0xFFFFFFFF)
            self.writeMsg(0x8002, _ping.pack(self.nonce))
            _log.debug("ping nonce: " + str(self.nonce))

    def getInitialState(self):
        return (self.recvHeader, 8)

    def recvHeader(self, data):
        self.restartPingTimer()
        magic, msgid, blen = _Head.unpack(data)
        if magic != _M:
            _log.error("Protocol error! Bad magic {magic}".format(magic=magic))
            self.transport.loseConnection()
            return
        self.msgid = msgid
        fn, minlen = self.rxfn[self.msgid]
        if minlen >= 0 and blen < minlen:
            return (self.ignoreBody, blen)
        else:
            return (fn, blen)

    # 0x0001
    def recvClientGreeting(self, body):
        cver, ctype, skey = _c_greet.unpack(body[: _c_greet.size])
        if ctype != 0:
            _log.error("I don't understand you! {s}".format(s=ctype))
            self.transport.loseConnection()
            return
        self.version = min(self.version, cver)
        self.clientKey = skey
        self.sess = self.factory.addClient(self, self.transport.getPeer())
        return self.getInitialState()

    # 0x0002
    def recvPong(self, body):
        (nonce,) = _ping.unpack(body[: _ping.size])
        if nonce != self.nonce:
            _log.error("pong nonce does not match! {pong_nonce}!={nonce}".format(pong_nonce=nonce, nonce=self.nonce))
            self.transport.loseConnection()
        else:
            _log.debug("pong nonce match")
            self.phase = 1
        return self.getInitialState()

    # 0x0006
    def recvInfo(self, body):
        record_id, klen, vlen = _c_info.unpack(body[: _c_info.size])
        text = body[_c_info.size :]
        text = text.decode()
        if klen == 0 or klen + vlen < len(text):
            _log.error("Ignoring info update")
            return self.getInitialState()
        key = text[:klen]
        val = text[klen : klen + vlen]
        if record_id:
            self.sess.recInfo(record_id, key, val)
        else:
            self.sess.iocInfo(key, val)
        return self.getInitialState()

    # 0x0003
    def recvAddRec(self, body):
        record_id, record_type, rtlen, rnlen = _c_rec.unpack(body[: _c_rec.size])
        text = body[_c_rec.size :]
        text = text.decode()
        if rnlen == 0 or rtlen + rnlen < len(text):
            _log.error("Ignoring record update")

        elif rtlen > 0 and record_type == 0:  # new record
            rectype = text[:rtlen]
            recname = text[rtlen : rtlen + rnlen]
            self.sess.addRecord(record_id, rectype, recname)

        elif record_type == 1:  # record alias
            recname = text[rtlen : rtlen + rnlen]
            self.sess.addAlias(record_id, recname)

        return self.getInitialState()

    # 0x0004
    def recvDelRec(self, body):
        record_id = _ping.unpack(body[: _ping.size])
        self.sess.delRecord(record_id)
        return self.getInitialState()

    # 0x0005
    def recvDone(self, body):
        self.factory.isDone(self, self.active)
        self.sess.done()
        if self.phase == 1:
            self.writePing()

        elapsed_s = time.time() - self.uploadStart
        size_kb = self.uploadSize / 1024
        rate_kbs = size_kb / elapsed_s
        source_address = "{}:{}".format(self.sess.ep.host, self.sess.ep.port)
        _log.info(
            "Done message from {source_address}: uploaded {size_kb}kB in {elapsed_s}s ({rate_kbs}kB/s)".format(
                source_address=source_address,
                size_kb=size_kb,
                elapsed_s=elapsed_s,
                rate_kbs=rate_kbs,
            )
        )

        return self.getInitialState()

    def ignoreBody(self, body):
        return self.getInitialState()

    @classmethod
    def dfact(cls):
        return (cls.ignoreBody, -1)


@implementer(ITransaction)
class Transaction(object):
    def __init__(self, ep, id):
        self.connected = True
        self.initial = False
        self.source_address = ep
        self.srcid = id
        self.records_to_add, self.client_infos, self.record_infos_to_add = {}, {}, {}
        self.aliases = collections.defaultdict(list)
        self.records_to_delete = set()

    def show(self, fp=sys.stdout):
        _log.info(str(self))

    def __str__(self):
        source_address = "{}:{}".format(self.source_address.host, self.source_address.port)
        init = self.initial
        conn = self.connected
        nenv = len(self.client_infos)
        nadd = len(self.records_to_add)
        ndel = len(self.records_to_delete)
        ninfo = len(self.record_infos_to_add)
        nalias = len(self.aliases)
        return "Transaction(Src:{}, Init:{}, Conn:{}, Env:{}, Rec:{}, Alias:{}, Info:{}, Del:{})".format(
            source_address, init, conn, nenv, nadd, nalias, ninfo, ndel
        )

    def __repr__(self):
        return f"""Transaction(
            source_address={self.source_address},
            initial={self.initial},
            connected={self.connected},
            records_to_add={self.records_to_add},
            client_infos={self.client_infos},
            record_infos_to_add={self.record_infos_to_add},
            aliases={self.aliases},
            records_to_delete={self.records_to_delete})
            """


class CollectionSession(object):
    timeout = 5.0
    trlimit = 0

    def __init__(self, proto, endpoint):
        from twisted.internet import reactor

        _log.info("Open session from {endpoint}".format(endpoint=endpoint))
        self.reactor = reactor
        self.proto, self.ep = proto, endpoint
        self.transaction = Transaction(self.ep, id(self))
        self.transaction.initial = True
        self.C = defer.succeed(None)
        self.T = None
        self.dirty = False

    def close(self):
        _log.info("Close session from {ep}".format(ep=self.ep))

        def suppressCancelled(err):
            if not err.check(defer.CancelledError):
                return err
            _log.debug("Suppress the expected CancelledError")

        self.C.addErrback(suppressCancelled).cancel()

        # Clear the current transaction and
        # commit an empty one for disconnect.
        self.transaction = Transaction(self.ep, id(self))
        self.transaction.connected = False
        self.dirty = True
        self.flush()

    def flush(self, connected=True):
        _log.info("Flush session from {s}".format(s=self.ep))
        self.T = None
        if not self.dirty:
            return

        transaction, self.transaction = self.transaction, Transaction(self.ep, id(self))
        self.dirty = False

        def commit(_ignored):
            _log.info("Commit: {transaction}".format(transaction=transaction))
            return defer.maybeDeferred(self.factory.commit, transaction)

        def abort(err):
            if err.check(defer.CancelledError):
                _log.info("Commit cancelled: {transaction}".format(transaction=transaction))
                return err
            else:
                _log.error("Commit failure: {err}".format(err=err))
                self.proto.transport.loseConnection()
                raise defer.CancelledError()

        self.C.addCallback(commit).addErrback(abort)

    # Flushes must NOT occur at arbitrary points in the data stream
    # because that can result in a PV and its record info or aliases being split
    # between transactions. Only flush after Add or Del or Done message received.
    def flushSafely(self):
        if self.T and self.T <= time.time():
            self.flush()
        elif self.trlimit and self.trlimit <= (
            len(self.transaction.records_to_add) + len(self.transaction.records_to_delete)
        ):
            self.flush()

    def markDirty(self):
        if not self.T:
            self.T = time.time() + self.timeout
        self.dirty = True

    def done(self):
        self.flush()

    def iocInfo(self, key, val):
        self.transaction.client_infos[key] = val
        self.markDirty()

    def addRecord(self, record_id, record_type, record_name):
        self.flushSafely()
        self.transaction.records_to_add[record_id] = (record_name, record_type)
        self.markDirty()

    def addAlias(self, record_id, record_name):
        self.transaction.aliases[record_id].append(record_name)
        self.markDirty()

    def delRecord(self, record_id):
        self.flushSafely()
        self.transaction.records_to_add.pop(record_id, None)
        self.transaction.records_to_delete.add(record_id)
        self.transaction.record_infos_to_add.pop(record_id, None)
        self.markDirty()

    def recInfo(self, record_id, key, val):
        try:
            client_infos = self.transaction.record_infos_to_add[record_id]
        except KeyError:
            client_infos = {}
            self.transaction.record_infos_to_add[record_id] = client_infos
        client_infos[key] = val
        self.markDirty()


class CastFactory(protocol.ServerFactory):
    protocol = CastReceiver
    session = CollectionSession

    maxActive = 3

    def __init__(self):
        # Flow control by limiting the number of concurrent
        # "active" connectons  Active means dumping lots of records.
        # connections become "inactive" by calling isDone()
        self.NActive = 0
        self.Wait = []

    def isDone(self, P, active):
        if not active:
            # connection closed before activation
            self.Wait.remove(P)
        elif len(self.Wait) > 0:
            # Others are waiting
            P2 = self.Wait.pop(0)
            P2.active = True
            P2.transport.resumeProducing()
            P2.connectionMade()
        else:
            self.NActive -= 1

    def buildProtocol(self, addr):
        active = self.NActive < self.maxActive
        P = self.protocol(active=active)
        P.factory = self
        if not active:
            self.Wait.append(P)
        return P

    def addClient(self, proto, address):
        S = self.session(proto, address)
        S.factory = self
        return S

    # Note: this method replaced by RecService
    def commit(self, transaction):
        transaction.show()
