# -*- coding: utf-8 -*-

import logging
import socket
from typing import Any

from zope.interface import implementer

from requests import RequestException, ConnectionError
from twisted.application import service
from twisted.internet.threads import deferToThread
from twisted.internet.defer import DeferredLock
from twisted.internet import defer
from operator import itemgetter
from collections import defaultdict
import time

from .processors import ConfigAdapter
from . import interfaces
import datetime
import os
import json
from channelfinder import ChannelFinderClient

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

DEFAULT_RECORD_PROPERTY_NAMES = {
    "hostName",
    "iocName",
    "pvStatus",
    "time",
    "iocid",
    "iocIP",
    RECCEIVERID_KEY,
}

Property = dict[str, str]
Tag = dict[str, str]
Record = dict[str, str | list[Tag] | list[Property]]


@implementer(interfaces.IProcessor)
class CFProcessor(service.Service):
    def __init__(self, name, conf: ConfigAdapter):
        _log.info("CF_INIT {name}".format(name=name))
        self.name: str = name
        self.conf: ConfigAdapter = conf
        self.records_dict = defaultdict(list)
        self.iocs: dict[str, dict[str, str | int]] = dict()
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
                cf_properties = self.fetch_cf_property_names()
                required_properties = DEFAULT_RECORD_PROPERTY_NAMES

                configured_properties = self.read_conf_properties(self.conf)
                record_property_names_list = self.read_conf_record_properties(self.conf)

                required_properties.update(configured_properties)
                # Are any required properties not already present on CF?
                properties = required_properties - set(cf_properties)
                # Are any whitelisted properties not already present on CF?
                # If so, add them too.
                properties.update(set(record_property_names_list) - set(cf_properties))

                owner: str = self.conf.get("username", "cfstore")
                for cf_property in properties:
                    self.client.set(property={"name": cf_property, "owner": owner})

                self.record_property_names_list = set(record_property_names_list)
                _log.debug(
                    "record_property_names_list = {}".format(
                        self.record_property_names_list
                    )
                )
            except ConnectionError:
                _log.exception("Cannot connect to Channelfinder service")
                raise
            else:
                if self.conf.getboolean("cleanOnStart", True):
                    self.clean_service()

    def read_conf_properties(self, conf: ConfigAdapter) -> set[str]:
        required_properties = set()
        if conf.get("alias"):
            required_properties.add("alias")

        if conf.get("recordType"):
            required_properties.add("recordType")

        env_var_properties = self.read_conf_env_vars(conf)
        required_properties.update(env_var_properties)

        # Standard property names for CA/PVA name server connections. These are
        # environment variables from reccaster so take advantage of env_vars
        if conf.get("iocConnectionInfo"):
            self.env_vars["RSRV_SERVER_PORT"] = "caPort"
            self.env_vars["PVAS_SERVER_PORT"] = "pvaPort"
            required_properties.add("caPort")
            required_properties.add("pvaPort")

        return required_properties

    def read_conf_env_vars(self, conf: ConfigAdapter) -> set[str]:
        required_properties = set()
        env_vars_setting = conf.get("environment_vars")
        self.env_vars: dict[str, str] = {}
        if env_vars_setting != "" and env_vars_setting is not None:
            env_vars_dict = dict(
                item.strip().split(":") for item in env_vars_setting.split(",")
            )
            self.env_vars = {k.strip(): v.strip() for k, v in env_vars_dict.items()}
            for epics_env_var_name, cf_prop_name in self.env_vars.items():
                required_properties.add(cf_prop_name)
        return required_properties

    def read_conf_record_properties(self, conf: ConfigAdapter) -> list[str]:
        configured_infotags = conf.get("infotags", list())
        if configured_infotags:
            record_property_names_list = [
                s.strip(", ") for s in configured_infotags.split()
            ]
        else:
            record_property_names_list = []

        if conf.get("recordDesc"):
            record_property_names_list.append("recordDesc")
        return record_property_names_list

    def fetch_cf_property_names(self) -> list[str]:
        cf_properties = [
            cf_property["name"] for cf_property in self.client.getAllProperties()
        ]

        return cf_properties

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

        host, iocName, hostName, owner, time, iocid = self.read_transaction_ioc_info(
            transaction
        )

        records_details = self.read_transaction_names_types(transaction)

        records_infos = self.read_transaction_record_infos_to_add(
            transaction, owner, iocid, records_details
        )
        for record_id, record_info in records_infos.items():
            records_details[record_id]["infoProperties"] = record_info

        record_aliases = self.read_transaction_aliases(
            transaction, iocid, records_details
        )
        for record_id, record_alias in record_aliases.items():
            records_details[record_id]["aliases"] = record_alias

        self.read_transaction_client_infos(transaction, iocName, owner, records_details)

        records_to_delete = list(transaction.records_to_delete)
        _log.debug("Delete records: {s}".format(s=records_to_delete))
        if not transaction.connected:
            records_to_delete.extend(self.records_dict.keys())

        record_details_by_name = self.convert_record_infos_to_by_name(
            iocid, records_details
        )

        if transaction.initial:
            self.add_ioc(host, iocName, hostName, owner, time, iocid)

        self.update_ioc_channel_counts_records_ioc_info(
            iocid, records_to_delete, record_details_by_name
        )

        poll(
            __updateCF__,
            self,
            record_details_by_name,
            records_to_delete,
            hostName,
            iocName,
            host,
            iocid,
            owner,
            time,
        )
        dict_to_file(self.records_dict, self.iocs, self.conf)

    def update_ioc_channel_counts_records_ioc_info(
        self,
        iocid: str,
        records_to_delete: list[str],
        record_details_by_name: dict[str, dict[str, Any]],
    ):
        for record_name in record_details_by_name.keys():
            self.records_dict[record_name].append(
                iocid
            )  # add iocname to pvName in dict
            self.iocs[iocid]["channelcount"] += 1
            """In case, alias exists"""
            if self.conf.get("alias"):
                if (
                    record_name in record_details_by_name
                    and "aliases" in record_details_by_name[record_name]
                ):
                    for alias in record_details_by_name[record_name]["aliases"]:
                        self.records_dict[alias].append(
                            iocid
                        )  # add iocname to pvName in dict
                        self.iocs[iocid]["channelcount"] += 1

        for record_name in records_to_delete:
            if iocid in self.records_dict[record_name]:
                self.remove_channel(record_name, iocid)
                """In case, alias exists"""
                if self.conf.get("alias"):
                    if (
                        record_name in record_details_by_name
                        and "aliases" in record_details_by_name[record_name]
                    ):
                        for alias in record_details_by_name[record_name]["aliases"]:
                            self.remove_channel(alias, iocid)

    def read_transaction_client_infos(
        self, transaction, iocName, owner, records_details
    ):
        for record_id in records_details:
            for epics_env_var_name, cf_prop_name in self.env_vars.items():
                if transaction.client_infos.get(epics_env_var_name) is not None:
                    property = {
                        "name": cf_prop_name,
                        "owner": owner,
                        "value": transaction.client_infos.get(epics_env_var_name),
                    }
                    if "infoProperties" not in records_details[record_id]:
                        records_details[record_id]["infoProperties"] = list()
                    records_details[record_id]["infoProperties"].append(property)
                else:
                    _log.debug(
                        "EPICS environment var {env_var} listed in environment_vars setting list not found in this IOC: {iocName}".format(
                            env_var=epics_env_var_name, iocName=iocName
                        )
                    )

    def convert_record_infos_to_by_name(
        self, iocid: str, recordInfo: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
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

        return recordInfoByName

    def add_ioc(self, host, iocName, hostName, owner, time, iocid):
        """Add IOC to source list"""
        self.iocs[iocid] = {
            "iocname": iocName,
            "hostname": hostName,
            "iocIP": host,
            "owner": owner,
            "time": time,
            "channelcount": 0,
        }

    def read_transaction_aliases(
        self,
        transaction: interfaces.ITransaction,
        iocid: str,
        recordInfo: dict[str, dict[str, Any]],
    ) -> dict[str, list[str]]:
        record_aliases = {}
        for record_id, alias in transaction.aliases.items():
            if record_id not in recordInfo:
                _log.warning(
                    "IOC: {iocid}: PV not found for alias with RID: {record_id}".format(
                        iocid=iocid, record_id=record_id
                    )
                )
                continue
            record_aliases[record_id] = alias
        return record_aliases

    def read_transaction_record_infos_to_add(
        self,
        transaction: interfaces.ITransaction,
        owner: str,
        iocid: str,
        recordInfo: dict[str, dict[str, str]],
    ) -> dict[str, list[Property]]:
        records_infos = {}
        for record_id, (record_infos_to_add) in transaction.record_infos_to_add.items():
            # find intersection of these sets
            if record_id not in recordInfo:
                _log.warning(
                    "IOC: {iocid}: PV not found for recinfo with RID: {record_id}".format(
                        iocid=iocid, record_id=record_id
                    )
                )
                continue
            recinfo_wl = [
                p
                for p in self.record_property_names_list
                if p in record_infos_to_add.keys()
            ]
            if recinfo_wl:
                records_infos[record_id] = list()
                for infotag in recinfo_wl:
                    property = {
                        "name": infotag,
                        "owner": owner,
                        "value": record_infos_to_add[infotag],
                    }
                    records_infos[record_id].append(property)

        return records_infos

    def read_transaction_names_types(
        self, transaction: interfaces.ITransaction
    ) -> dict[str, dict[str, Any]]:
        recordInfo = {}
        for record_id, (record_name, record_type) in transaction.records_to_add.items():
            recordInfo[record_id] = {"pvName": record_name}
            if self.conf.get("recordType", "default" == "on"):
                recordInfo[record_id]["recordType"] = record_type
        return recordInfo

    def read_transaction_ioc_info(self, transaction: interfaces.ITransaction):
        host: str = transaction.source_address.host
        port: int = transaction.source_address.port
        iocName: str = (
            transaction.client_infos.get("IOCNAME") or transaction.source_address.port
        )
        hostName: str = (
            transaction.client_infos.get("HOSTNAME") or transaction.source_address.host
        )
        owner: str = (
            transaction.client_infos.get("ENGINEER")
            or transaction.client_infos.get("CF_USERNAME")
            or self.conf.get("username", "cfstore")
        )
        time: str = self.currentTime(timezone=self.conf.get("timezone", None))

        """The unique identifier for a particular IOC"""
        iocid: str = host + ":" + str(port)
        return host, iocName, hostName, owner, time, iocid

    def remove_channel(self, recordName, iocid):
        self.records_dict[recordName].remove(iocid)
        if iocid in self.iocs:
            self.iocs[iocid]["channelcount"] -= 1
        if self.iocs[iocid]["channelcount"] == 0:
            self.iocs.pop(iocid, None)
        elif self.iocs[iocid]["channelcount"] < 0:
            _log.error("Channel count negative: {s}", s=iocid)
        if len(self.records_dict[recordName]) <= 0:  # case: record has no more iocs
            del self.records_dict[recordName]

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
            _log.info(
                "Clean service retry in {retry_seconds} seconds".format(
                    retry_seconds=retry_seconds
                )
            )
            time.sleep(retry_seconds)
            sleep *= 1.5
            if self.running == 0 and sleep >= retry_limit:
                _log.info(
                    "Abandoning clean after {retry_limit} seconds".format(
                        retry_limit=retry_limit
                    )
                )
                return

    def get_active_channels(self, recceiverid):
        return self.client.findByArgs(
            prepareFindArgs(
                self.conf, [("pvStatus", "Active"), (RECCEIVERID_KEY, recceiverid)]
            )
        )

    def clean_channels(self, owner, records):
        new_channels = []
        for cf_record in records or []:
            new_channels.append(cf_record["name"])
        _log.info(
            "Total records to update: {nChannels}".format(nChannels=len(new_channels))
        )
        _log.debug(
            'Update "pvStatus" property to "Inactive" for {n_channels} records'.format(
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
            list.append(
                [key, iocs[dict[key][-1]]["hostname"], iocs[dict[key][-1]]["iocname"]]
            )

        list.sort(key=itemgetter(0))

        with open(filename, "w+") as f:
            json.dump(list, f)


def __updateCF__(
    processor: CFProcessor,
    recordInfoByName: dict[str, dict[str, Any]],
    records_to_delete,
    hostName: str,
    iocName: str,
    iocIP: str,
    iocid: str,
    owner: str,
    iocTime: str,
):
    _log.info("CF Update IOC: {iocid}".format(iocid=iocid))

    # Consider making this function a class methed then 'processor' simply becomes 'self'
    client = processor.client
    records_dict = processor.records_dict
    iocs = processor.iocs
    conf = processor.conf
    recceiverid = conf.get(RECCEIVERID_KEY, RECCEIVERID_DEFAULT)
    new_records = set(recordInfoByName.keys())

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
    old_records = client.findByArgs(prepareFindArgs(conf, [("iocid", iocid)]))
    if processor.cancelled:
        raise defer.CancelledError()

    if old_records is not None:
        for cf_record in old_records:
            if (
                len(new_records) == 0 or cf_record["name"] in records_to_delete
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
                            for alias in recordInfoByName[cf_record["name"]]["aliases"]:
                                if alias["name"] in records_dict:
                                    alias["owner"] = iocs[
                                        records_dict[alias["name"]][-1]
                                    ]["owner"]
                                    alias["properties"] = __merge_property_lists(
                                        ch_create_properties(
                                            owner,
                                            iocTime,
                                            recceiverid,
                                            records_dict,
                                            iocs,
                                            cf_record,
                                        ),
                                        alias["properties"],
                                    )
                                    if conf.get("recordType", "default") == "on":
                                        cf_record["properties"] = (
                                            __merge_property_lists(
                                                cf_record["properties"].append(
                                                    {
                                                        "name": "recordType",
                                                        "owner": owner,
                                                        "value": iocs[
                                                            records_dict[alias["name"]][
                                                                -1
                                                            ]
                                                        ]["recordType"],
                                                    }
                                                ),
                                                cf_record["properties"],
                                            )
                                        )
                                    records.append(alias)
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
                            for alias in recordInfoByName[cf_record["name"]]["aliases"]:
                                alias["properties"] = __merge_property_lists(
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
                                    alias["properties"],
                                )
                                records.append(alias)
                                _log.debug(
                                    "Add orphaned alias with no IOC: {s}".format(
                                        s=records[-1]
                                    )
                                )
            else:
                if cf_record["name"] in new_records:  # case: record in old and new
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
                    new_records.remove(cf_record["name"])

                    """In case, alias exist"""
                    if conf.get("alias", "default") == "on":
                        if (
                            cf_record["name"] in recordInfoByName
                            and "aliases" in recordInfoByName[cf_record["name"]]
                        ):
                            for alias in recordInfoByName[cf_record["name"]]["aliases"]:
                                if alias in old_records:
                                    """alias exists in old list"""
                                    alias["properties"] = __merge_property_lists(
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
                                        alias["properties"],
                                    )
                                    records.append(alias)
                                    new_records.remove(alias["name"])
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
                                            "name": alias["name"],
                                            "owner": owner,
                                            "properties": aprops,
                                        }
                                    )
                                    new_records.remove(alias["name"])
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
    for record_name in new_records:
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

    for record_name in new_records:
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
                    for alias in recordInfoByName[record_name]["aliases"]:
                        if alias in existingChannels:
                            ach = existingChannels[alias]
                            ach["properties"] = __merge_property_lists(
                                alProps, ach["properties"]
                            )
                            records.append(ach)
                        else:
                            records.append(
                                {"name": alias, "owner": owner, "properties": alProps}
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
                    for alias in recordInfoByName[record_name]["aliases"]:
                        records.append(
                            {"name": alias, "owner": owner, "properties": alProps}
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
        if old_records and len(old_records) != 0:
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
    processor: CFProcessor,
    recordInfoByName: dict[str, dict[str, Any]],
    records_to_delete,
    hostName: str,
    iocName: str,
    iocIP: str,
    iocid: str,
    owner: str,
    iocTime: str,
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
            _log.info(
                "ChannelFinder update retry in {retry_seconds} seconds".format(
                    retry_seconds=retry_seconds
                )
            )
            time.sleep(retry_seconds)
            sleep *= 1.5
    _log.info("Polling {iocName} complete".format(iocName=iocName))
