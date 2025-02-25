# -*- coding: utf-8 -*-

import datetime
import json
import logging
import os
import socket
import time
from collections import defaultdict
from operator import itemgetter

from channelfinder import ChannelFinderClient
from requests import ConnectionError, RequestException
from zope.interface import implementer

from twisted.application import service
from twisted.internet import defer
from twisted.internet.defer import DeferredLock
from twisted.internet.threads import deferToThread

from . import interfaces

_log = logging.getLogger(__name__)

# ITRANSACTION FORMAT:
#
# source_address = source address
# records_to_add = records ein added ( recname, rectype, {key:val})
# records_to_delete = a set() of records which are being removed
# client_infos = dictionary of client client_infos
# record_infos_to_add = additional client_infos being added to existing records
# "recid: {key:value}"
#

__all__ = ["CFProcessor"]

RECCEIVERID_KEY = "recceiverID"
RECCEIVERID_DEFAULT = socket.gethostname()


@implementer(interfaces.IProcessor)
class CFProcessor(service.Service):
    def __init__(self, name, conf):
        _log.info("CF_INIT {name}".format(name=name))
        self.name, self.conf = name, conf
        self.records_dict = defaultdict(list)
        self.iocs = dict()
        self.client = None
        self.currentTime = getCurrentTime
        self.lock = DeferredLock()

    def startService(self):
        service.Service.startService(self)
        # Returning a Deferred is not supported by startService(),
        # so instead attempt to acquire the lock synchonously!
        d = self.lock.acquire()
        if not d.called:
            d.cancel()
            service.Service.stopService(self)
            raise RuntimeError("Failed to acquired CF Processor lock for service start")

        try:
            self._startServiceWithLock()
        except:
            service.Service.stopService(self)
            raise
        finally:
            self.lock.release()

    def _startServiceWithLock(self):
        _log.info("CF_START")

        if self.client is None:  # For setting up mock test client
            """
            Using the default python cf-client.  The url, username, and
            password are provided by the channelfinder._conf module.
            """
            self.client = ChannelFinderClient()
            try:
                cf_properties = [
                    cf_property["name"]
                    for cf_property in self.client.getAllProperties()
                ]
                required_properties = {
                    "hostName",
                    "iocName",
                    "pvStatus",
                    "time",
                    "iocid",
                    "iocIP",
                    RECCEIVERID_KEY,
                }

                if self.conf.get("alias"):
                    required_properties.add("alias")
                if self.conf.get("recordType"):
                    required_properties.add("recordType")
                env_vars_setting = self.conf.get("environment_vars")
                self.env_vars = {}
                if env_vars_setting != "" and env_vars_setting is not None:
                    env_vars_dict = dict(item.strip().split(":") for item in env_vars_setting.split(","))
                    self.env_vars = {k.strip(): v.strip() for k, v in env_vars_dict.items()}
                    for epics_env_var_name, cf_prop_name in self.env_vars.items():
                        required_properties.add(cf_prop_name)
                # Standard property names for CA/PVA name server connections. These are
                # environment variables from reccaster so take advantage of env_vars
                if self.conf.get("iocConnectionInfo"):
                    self.env_vars["RSRV_SERVER_PORT"] = "caPort"
                    self.env_vars["PVAS_SERVER_PORT"] = "pvaPort"
                    required_properties.add("caPort")
                    required_properties.add("pvaPort")
                wl = self.conf.get("infotags", list())
                if wl:
                    whitelist = [s.strip(", ") for s in wl.split()]
                else:
                    whitelist = []
                if self.conf.get("recordDesc"):
                    whitelist.append("recordDesc")
                # Are any required properties not already present on CF?
                properties = required_properties - set(cf_properties)
                # Are any whitelisted properties not already present on CF?
                # If so, add them too.
                properties.update(set(whitelist) - set(cf_properties))

                owner = self.conf.get("username", "cfstore")
                for cf_property in properties:
                    self.client.set(property={"name": cf_property, "owner": owner})

                self.whitelist = set(whitelist)
                _log.debug("WHITELIST = {}".format(self.whitelist))
            except ConnectionError:
                _log.exception("Cannot connect to Channelfinder service")
                raise
            else:
                if self.conf.getboolean("cleanOnStart", True):
                    self.clean_service()

    def stopService(self):
        _log.info("CF_STOP")
        service.Service.stopService(self)
        return self.lock.run(self._stopServiceWithLock)

    def _stopServiceWithLock(self):
        # Set records to inactive and close connection to client
        if self.conf.getboolean("cleanOnStop", True):
            self.clean_service()
        _log.info("CF_STOP with lock")

    # @defer.inlineCallbacks # Twisted v16 does not support cancellation!
    def commit(self, transaction_record):
        return self.lock.run(self._commitWithLock, transaction_record)

    def _commitWithLock(self, transaction):
        self.cancelled = False

        t = deferToThread(self._commitWithThread, transaction)

        def cancelCommit(d):
            self.cancelled = True
            d.callback(None)

        d = defer.Deferred(cancelCommit)

        def waitForThread(_ignored):
            if self.cancelled:
                return t

        d.addCallback(waitForThread)

        def chainError(err):
            if not err.check(defer.CancelledError):
                _log.error("CF_COMMIT FAILURE: {s}".format(s=err))
            if self.cancelled:
                if not err.check(defer.CancelledError):
                    raise defer.CancelledError()
                return err
            else:
                d.callback(None)

        def chainResult(_ignored):
            if self.cancelled:
                raise defer.CancelledError()
            else:
                d.callback(None)

        t.addCallbacks(chainResult, chainError)
        return d

    def _commitWithThread(self, transaction):
        if not self.running:
            raise defer.CancelledError(
                "CF Processor is not running (transaction: {host}:{port})",
                host=transaction.source_address.host,
                port=transaction.source_address.port,
            )

        _log.info("CF_COMMIT: {transaction}".format(transaction=transaction))
        """
        a dictionary with a list of records with their associated property info
        pvInfo
        {record_id: { "pvName":"recordName",
                "infoProperties":{propName:value, ...}}}
        """

        host = transaction.source_address.host
        port = transaction.source_address.port
        iocName = (
            transaction.client_infos.get("IOCNAME") or transaction.source_address.port
        )
        hostName = (
            transaction.client_infos.get("HOSTNAME") or transaction.source_address.host
        )
        owner = (
            transaction.client_infos.get("ENGINEER")
            or transaction.client_infos.get("CF_USERNAME")
            or self.conf.get("username", "cfstore")
        )
        time = self.currentTime(timezone=self.conf.get("timezone"))

        """The unique identifier for a particular IOC"""
        iocid = host + ":" + str(port)

        recordInfo = {}
        for record_id, (rname, rtype) in transaction.records_to_add.items():
            recordInfo[record_id] = {"pvName": rname}
            if self.conf.get("recordType"):
                recordInfo[record_id]["recordType"] = rtype
        for record_id, (record_infos_to_add) in transaction.record_infos_to_add.items():
            # find intersection of these sets
            if record_id not in recordInfo:
                _log.warning(
                    "IOC: {iocid}: PV not found for recinfo with RID: {record_id}".format(
                        iocid=iocid, record_id=record_id
                    )
                )
                continue
            recinfo_wl = [p for p in self.whitelist if p in record_infos_to_add.keys()]
            if recinfo_wl:
                recordInfo[record_id]["infoProperties"] = list()
                for infotag in recinfo_wl:
                    property = {
                        "name": infotag,
                        "owner": owner,
                        "value": record_infos_to_add[infotag],
                    }
                    recordInfo[record_id]["infoProperties"].append(property)

        for record_id, alias in transaction.aliases.items():
            if record_id not in recordInfo:
                _log.warning(
                    "IOC: {iocid}: PV not found for alias with RID: {record_id}".format(
                        iocid=iocid, record_id=record_id
                    )
                )
                continue
            recordInfo[record_id]["aliases"] = alias

        for record_id in recordInfo:
            for epics_env_var_name, cf_prop_name in self.env_vars.items():
                if transaction.client_infos.get(epics_env_var_name) is not None:
                    property = {
                        "name": cf_prop_name,
                        "owner": owner,
                        "value": transaction.client_infos.get(epics_env_var_name),
                    }
                    if "infoProperties" not in recordInfo[record_id]:
                        recordInfo[record_id]["infoProperties"] = list()
                    recordInfo[record_id]["infoProperties"].append(property)
                else:
                    _log.debug(
                        "EPICS environment var %s listed in environment_vars setting list not found in this IOC: %s",
                        epics_env_var_name,
                        iocName,
                    )

        records_to_delete = list(transaction.records_to_delete)
        _log.debug("Delete records: {s}".format(s=records_to_delete))

        recordInfoByName = {}
        for record_id, (info) in recordInfo.items():
            if info["pvName"] in recordInfoByName:
                _log.warning(
                    "Commit contains multiple records with PV name: {pv} ({iocid})".format(
                        pv=info["pvName"], iocid=iocid
                    )
                )
                continue
            recordInfoByName[info["pvName"]] = info
            _log.debug(
                "Add record: {record_id}: {info}".format(record_id=record_id, info=info)
            )

        if transaction.initial:
            """Add IOC to source list """
            self.iocs[iocid] = {
                "iocname": iocName,
                "hostname": hostName,
                "iocIP": host,
                "owner": owner,
                "time": time,
                "channelcount": 0,
            }
        if not transaction.connected:
            records_to_delete.extend(self.records_dict.keys())
        for record_name in recordInfoByName.keys():
            self.records_dict[record_name].append(
                iocid
            )
            self.iocs[iocid]["channelcount"] += 1
            """In case, alias exists"""
            if self.conf.get("alias"):
                if (
                    record_name in recordInfoByName
                    and "aliases" in recordInfoByName[record_name]
                ):
                    for a in recordInfoByName[record_name]["aliases"]:
                        self.records_dict[a].append(
                            iocid
                        )  # add iocname to pvName in dict
                        self.iocs[iocid]["channelcount"] += 1
        for record_name in records_to_delete:
            if iocid in self.records_dict[record_name]:
                self.remove_channel(record_name, iocid)
                """In case, alias exists"""
                if self.conf.get("alias"):
                    if (
                        record_name in recordInfoByName
                        and "aliases" in recordInfoByName[record_name]
                    ):
                        for a in recordInfoByName[record_name]["aliases"]:
                            self.remove_channel(a, iocid)
        poll(
            __updateCF__,
            self,
            recordInfoByName,
            records_to_delete,
            hostName,
            iocName,
            host,
            iocid,
            owner,
            time,
        )
        dict_to_file(self.records_dict, self.iocs, self.conf)

    def remove_channel(self, a, iocid):
        self.records_dict[a].remove(iocid)
        if iocid in self.iocs:
            self.iocs[iocid]["channelcount"] -= 1
        if self.iocs[iocid]["channelcount"] == 0:
            self.iocs.pop(iocid, None)
        elif self.iocs[iocid]["channelcount"] < 0:
            _log.error("Channel count negative: {s}", s=iocid)
        if len(self.records_dict[a]) <= 0:  # case: record has no more iocs
            del self.records_dict[a]

    def clean_service(self):
        """
        Marks all records as "Inactive" until the recsync server is back up
        """
        sleep = 1
        retry_limit = 5
        owner = self.conf.get("username", "cfstore")
        recceiverid = self.conf.get(RECCEIVERID_KEY, RECCEIVERID_DEFAULT)
        while 1:
            try:
                _log.info("CF Clean Started")
                records = self.get_active_channels(recceiverid)
                if records is not None:
                    while records is not None and len(records) > 0:
                        self.clean_channels(owner, records)
                        records = self.get_active_channels(recceiverid)
                    _log.info("CF Clean Completed")
                    return
                else:
                    _log.info("CF Clean Completed")
                    return
            except RequestException as e:
                _log.error("Clean service failed: {s}".format(s=e))
            retry_seconds = min(60, sleep)
            _log.info("Clean service retry in {retry_seconds} seconds".format(retry_seconds=retry_seconds))
            time.sleep(retry_seconds)
            sleep *= 1.5
            if self.running == 0 and sleep >= retry_limit:
                _log.info("Abandoning clean after {retry_limit} seconds".format(retry_limit=retry_limit))
                return

    def get_active_channels(self, recceiverid):
        return self.client.findByArgs(
            prepareFindArgs(self.conf, [("pvStatus", "Active"), (RECCEIVERID_KEY, recceiverid)])
        )

    def clean_channels(self, owner, records):
        new_channels = []
        for cf_record in records or []:
            new_channels.append(cf_record["name"])
        _log.info(
            "Total channels to update: {nChannels}".format(nChannels=len(new_channels))
        )
        _log.debug(
            'Update "pvStatus" property to "Inactive" for {n_channels} channels'.format(
                n_channels=len(new_channels)
            )
        )
        self.client.update(
            property={"name": "pvStatus", "owner": owner, "value": "Inactive"},
            channelNames=new_channels,
        )


def dict_to_file(dict, iocs, conf):
    filename = conf.get("debug_file_loc", None)
    if filename:
        if os.path.isfile(filename):
            os.remove(filename)
        list = []
        for key in dict:
            list.append([key, iocs[dict[key][-1]]["hostname"], iocs[dict[key][-1]]["iocname"]])

        list.sort(key=itemgetter(0))

        with open(filename, "w+") as f:
            json.dump(list, f)


def __updateCF__(
    processor,
    recordInfoByName,
    records_to_delete,
    hostName,
    iocName,
    iocIP,
    iocid,
    owner,
    iocTime,
):
    _log.info("CF Update IOC: {iocid}".format(iocid=iocid))

    # Consider making this function a class methed then 'processor' simply becomes 'self'
    client = processor.client
    records_dict = processor.records_dict
    iocs = processor.iocs
    conf = processor.conf
    recceiverid = conf.get(RECCEIVERID_KEY, RECCEIVERID_DEFAULT)
    new = set(recordInfoByName.keys())

    if iocid in iocs:
        hostName = iocs[iocid]["hostname"]
        iocName = iocs[iocid]["iocname"]
        owner = iocs[iocid]["owner"]
        iocTime = iocs[iocid]["time"]
        iocIP = iocs[iocid]["iocIP"]
    else:
        _log.warning("IOC Env Info not found: {iocid}".format(iocid=iocid))

    if hostName is None or iocName is None:
        raise Exception("missing hostName or iocName")

    if processor.cancelled:
        raise defer.CancelledError()

    records = []
    """A list of records in channelfinder with the associated hostName and iocName"""
    _log.debug("Find existing records by IOCID: {iocid}".format(iocid=iocid))
    old = client.findByArgs(prepareFindArgs(conf, [("iocid", iocid)]))
    if processor.cancelled:
        raise defer.CancelledError()

    if old is not None:
        for cf_record in old:
            if (
                len(new) == 0 or cf_record["name"] in records_to_delete
            ):  # case: empty commit/del, remove all reference to ioc
                if cf_record["name"] in records_dict:
                    cf_record["owner"] = iocs[records_dict[cf_record["name"]][-1]][
                        "owner"
                    ]
                    cf_record["properties"] = __merge_property_lists(
                        ch_create_properties(
                            owner, iocTime, recceiverid, records_dict, iocs, cf_record
                        ),
                        cf_record["properties"],
                    )
                    if conf.get("recordType"):
                        cf_record["properties"] = __merge_property_lists(
                            cf_record["properties"].append(
                                {
                                    "name": "recordType",
                                    "owner": owner,
                                    "value": iocs[records_dict[cf_record["name"]][-1]][
                                        "recordType"
                                    ],
                                }
                            ),
                            cf_record["properties"],
                        )
                    records.append(cf_record)
                    _log.debug(
                        "Add existing record to previous IOC: {s}".format(s=records[-1])
                    )
                    """In case alias exist, also delete them"""
                    if conf.get("alias"):
                        if (
                            cf_record["name"] in recordInfoByName
                            and "aliases" in recordInfoByName[cf_record["name"]]
                        ):
                            for a in recordInfoByName[cf_record["name"]]["aliases"]:
                                if a["name"] in records_dict:
                                    a["owner"] = iocs[records_dict[a["name"]][-1]][
                                        "owner"
                                    ]
                                    a["properties"] = __merge_property_lists(
                                        ch_create_properties(
                                            owner,
                                            iocTime,
                                            recceiverid,
                                            records_dict,
                                            iocs,
                                            cf_record,
                                        ),
                                        a["properties"],
                                    )
                                    if conf.get("recordType", "default") == "on":
                                        cf_record["properties"] = (
                                            __merge_property_lists(
                                                cf_record["properties"].append(
                                                    {
                                                        "name": "recordType",
                                                        "owner": owner,
                                                        "value": iocs[
                                                            records_dict[a["name"]][-1]
                                                        ]["recordType"],
                                                    }
                                                ),
                                                cf_record["properties"],
                                            )
                                        )
                                    records.append(a)
                                    _log.debug(
                                        "Add existing alias to previous IOC: {s}".format(
                                            s=records[-1]
                                        )
                                    )

                else:
                    """Orphan the record : mark as inactive, keep the old hostName and iocName"""
                    cf_record["properties"] = __merge_property_lists(
                        [
                            {"name": "pvStatus", "owner": owner, "value": "Inactive"},
                            {"name": "time", "owner": owner, "value": iocTime},
                        ],
                        cf_record["properties"],
                    )
                    records.append(cf_record)
                    _log.debug(
                        "Add orphaned record with no IOC: {s}".format(s=records[-1])
                    )
                    """Also orphan any alias"""
                    if conf.get("alias", "default") == "on":
                        if (
                            cf_record["name"] in recordInfoByName
                            and "aliases" in recordInfoByName[cf_record["name"]]
                        ):
                            for a in recordInfoByName[cf_record["name"]]["aliases"]:
                                a["properties"] = __merge_property_lists(
                                    [
                                        {
                                            "name": "pvStatus",
                                            "owner": owner,
                                            "value": "Inactive",
                                        },
                                        {
                                            "name": "time",
                                            "owner": owner,
                                            "value": iocTime,
                                        },
                                    ],
                                    a["properties"],
                                )
                                records.append(a)
                                _log.debug(
                                    "Add orphaned alias with no IOC: {s}".format(
                                        s=records[-1]
                                    )
                                )
            else:
                if cf_record["name"] in new:  # case: record in old and new
                    """
                    Channel exists in Channelfinder with same hostname and iocname.
                    Update the status to ensure it is marked active and update the time.
                    """
                    cf_record["properties"] = __merge_property_lists(
                        [
                            {"name": "pvStatus", "owner": owner, "value": "Active"},
                            {"name": "time", "owner": owner, "value": iocTime},
                        ],
                        cf_record["properties"],
                    )
                    records.append(cf_record)
                    _log.debug(
                        "Add existing record with same IOC: {s}".format(s=records[-1])
                    )
                    new.remove(cf_record["name"])

                    """In case, alias exist"""
                    if conf.get("alias", "default") == "on":
                        if (
                            cf_record["name"] in recordInfoByName
                            and "aliases" in recordInfoByName[cf_record["name"]]
                        ):
                            for a in recordInfoByName[cf_record["name"]]["aliases"]:
                                if a in old:
                                    """alias exists in old list"""
                                    a["properties"] = __merge_property_lists(
                                        [
                                            {
                                                "name": "pvStatus",
                                                "owner": owner,
                                                "value": "Active",
                                            },
                                            {
                                                "name": "time",
                                                "owner": owner,
                                                "value": iocTime,
                                            },
                                        ],
                                        a["properties"],
                                    )
                                    records.append(a)
                                    new.remove(a["name"])
                                else:
                                    """alias exists but not part of old list"""
                                    aprops = __merge_property_lists(
                                        [
                                            {
                                                "name": "pvStatus",
                                                "owner": owner,
                                                "value": "Active",
                                            },
                                            {
                                                "name": "time",
                                                "owner": owner,
                                                "value": iocTime,
                                            },
                                            {
                                                "name": "alias",
                                                "owner": owner,
                                                "value": cf_record["name"],
                                            },
                                        ],
                                        cf_record["properties"],
                                    )
                                    records.append(
                                        {
                                            "name": a["name"],
                                            "owner": owner,
                                            "properties": aprops,
                                        }
                                    )
                                    new.remove(a["name"])
                                _log.debug(
                                    "Add existing alias with same IOC: {s}".format(
                                        s=records[-1]
                                    )
                                )
    # now pvNames contains a list of pv's new on this host/ioc
    """A dictionary representing the current channelfinder information associated with the pvNames"""
    existingChannels = {}

    """
    The list of pv's is searched keeping in mind the limitations on the URL length
    The search is split into groups to ensure that the size does not exceed 600 characters
    """
    searchStrings = []
    searchString = ""
    for record_name in new:
        if not searchString:
            searchString = record_name
        elif len(searchString) + len(record_name) < 600:
            searchString = searchString + "|" + record_name
        else:
            searchStrings.append(searchString)
            searchString = record_name
    if searchString:
        searchStrings.append(searchString)

    for eachSearchString in searchStrings:
        _log.debug(
            "Find existing records by name: {search}".format(search=eachSearchString)
        )
        for cf_record in client.findByArgs(
            prepareFindArgs(conf, [("~name", eachSearchString)])
        ):
            existingChannels[cf_record["name"]] = cf_record
        if processor.cancelled:
            raise defer.CancelledError()

    for record_name in new:
        newProps = create_properties(
            owner, iocTime, recceiverid, hostName, iocName, iocIP, iocid
        )
        if conf.get("recordType", "default") == "on":
            newProps.append(
                {
                    "name": "recordType",
                    "owner": owner,
                    "value": recordInfoByName[record_name]["recordType"],
                }
            )
        if (
            record_name in recordInfoByName
            and "infoProperties" in recordInfoByName[record_name]
        ):
            newProps = newProps + recordInfoByName[record_name]["infoProperties"]

        if record_name in existingChannels:
            """update existing record: exists but with a different hostName and/or iocName"""
            existingChannel = existingChannels[record_name]
            existingChannel["properties"] = __merge_property_lists(
                newProps, existingChannel["properties"]
            )
            records.append(existingChannel)
            _log.debug(
                "Add existing record with different IOC: {s}".format(s=records[-1])
            )
            """in case, alias exists, update their properties too"""
            if conf.get("alias", "default") == "on":
                if (
                    record_name in recordInfoByName
                    and "aliases" in recordInfoByName[record_name]
                ):
                    alProps = [{"name": "alias", "owner": owner, "value": record_name}]
                    for p in newProps:
                        alProps.append(p)
                    for a in recordInfoByName[record_name]["aliases"]:
                        if a in existingChannels:
                            ach = existingChannels[a]
                            ach["properties"] = __merge_property_lists(
                                alProps, ach["properties"]
                            )
                            records.append(ach)
                        else:
                            records.append(
                                {"name": a, "owner": owner, "properties": alProps}
                            )
                        _log.debug(
                            "Add existing alias with different IOC: {s}".format(
                                s=records[-1]
                            )
                        )

        else:
            """New record"""
            records.append(
                {"name": record_name, "owner": owner, "properties": newProps}
            )
            _log.debug("Add new record: {s}".format(s=records[-1]))
            if conf.get("alias", "default") == "on":
                if (
                    record_name in recordInfoByName
                    and "aliases" in recordInfoByName[record_name]
                ):
                    alProps = [{"name": "alias", "owner": owner, "value": record_name}]
                    for p in newProps:
                        alProps.append(p)
                    for a in recordInfoByName[record_name]["aliases"]:
                        records.append(
                            {"name": a, "owner": owner, "properties": alProps}
                        )
                        _log.debug("Add new alias: {s}".format(s=records[-1]))
    _log.info(
        "Total records to update: {nChannels} {iocName}".format(
            nChannels=len(records), iocName=iocName
        )
    )
    if len(records) != 0:
        client.set(channels=records)
    else:
        if old and len(old) != 0:
            client.set(channels=records)
    if processor.cancelled:
        raise defer.CancelledError()


def create_properties(owner, iocTime, recceiverid, hostName, iocName, iocIP, iocid):
    return [
        {"name": "hostName", "owner": owner, "value": hostName},
        {"name": "iocName", "owner": owner, "value": iocName},
        {"name": "iocid", "owner": owner, "value": iocid},
        {"name": "iocIP", "owner": owner, "value": iocIP},
        {"name": "pvStatus", "owner": owner, "value": "Active"},
        {"name": "time", "owner": owner, "value": iocTime},
        {"name": RECCEIVERID_KEY, "owner": owner, "value": recceiverid},
    ]


def ch_create_properties(owner, iocTime, recceiverid, records_dict, iocs, cf_record):
    return create_properties(
        owner,
        iocTime,
        recceiverid,
        iocs[records_dict[cf_record["name"]][-1]]["hostname"],
        iocs[records_dict[cf_record["name"]][-1]]["iocname"],
        iocs[records_dict[cf_record["name"]][-1]]["iocIP"],
        records_dict[cf_record["name"]][-1],
    )


def __merge_property_lists(newProperties, oldProperties):
    """
    Merges two lists of properties ensuring that there are no 2 properties with
    the same name In case of overlap between the new and old property lists the
    new property list wins out
    """
    newPropNames = [p["name"] for p in newProperties]
    for oldProperty in oldProperties:
        if oldProperty["name"] not in newPropNames:
            newProperties = newProperties + [oldProperty]
    return newProperties


def getCurrentTime(timezone=False):
    if timezone:
        return str(datetime.datetime.now().astimezone())
    return str(datetime.datetime.now())


def prepareFindArgs(conf, args, size=0):
    size_limit = int(conf.get("findSizeLimit", size))
    if size_limit > 0:
        args.append(("~size", size_limit))
    return args


def poll(
    update_method,
    processor,
    recordInfoByName,
    records_to_delete,
    hostName,
    iocName,
    iocIP,
    iocid,
    owner,
    iocTime,
):
    _log.info("Polling {iocName} begins...".format(iocName=iocName))
    sleep = 1
    success = False
    while not success:
        try:
            update_method(
                processor,
                recordInfoByName,
                records_to_delete,
                hostName,
                iocName,
                iocIP,
                iocid,
                owner,
                iocTime,
            )
            success = True
            return success
        except RequestException as e:
            _log.error("ChannelFinder update failed: {s}".format(s=e))
            retry_seconds = min(60, sleep)
            _log.info("ChannelFinder update retry in {retry_seconds} seconds".format(retry_seconds=retry_seconds))
            time.sleep(retry_seconds)
            sleep *= 1.5
    _log.info("Polling {iocName} complete".format(iocName=iocName))
