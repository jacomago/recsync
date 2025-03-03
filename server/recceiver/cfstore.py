# -*- coding: utf-8 -*-

import logging
import socket
from typing import Any, List, Set, Dict

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


class CFStoreException(Exception):
    """Base exception class for CFStore errors."""

    pass


class ConfigurationError(CFStoreException):
    """Raised when there is an error in configuration."""

    pass


class ChannelFinderError(CFStoreException):
    """Raised when there is an error communicating with ChannelFinder service."""

    pass


class ValidationError(CFStoreException):
    """Raised when there is an error validating data."""

    pass


class ResourceError(CFStoreException):
    """Raised when there is an error managing resources."""

    pass


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
        """Initialize the CFProcessor service.

        Args:
            name: Name of the processor
            conf: Configuration adapter containing settings

        Raises:
            ConfigurationError: If required configuration is missing
            ValidationError: If configuration values are invalid
        """
        try:
            _log.info("CF_INIT {name}".format(name=name))
            self.validate_configuration(conf)

            self.name: str | None = name
            self.conf: ConfigAdapter = conf
            self.records_dict = defaultdict(list)
            self.iocs: dict[str, dict[str, str | int]] = dict()
            self.client: ChannelFinderClient = None
            self.currentTime = getCurrentTime
            self.lock = DeferredLock()
        except Exception as e:
            raise ConfigurationError(
                f"Failed to initialize CFProcessor: {str(e)}"
            ) from e

    def validate_configuration(self, conf: ConfigAdapter) -> None:
        """Validate the configuration settings.

        Args:
            conf: Configuration adapter to validate

        Raises:
            ValidationError: If required settings are missing or invalid
        """
        required_settings = ["username"]
        for setting in required_settings:
            if not conf.get(setting):
                raise ValidationError(
                    f"Missing required configuration setting: {setting}"
                )

    def startService(self):
        """Start the CFProcessor service.

        Raises:
            ResourceError: If unable to acquire required resources
            ChannelFinderError: If unable to connect to ChannelFinder service
        """
        service.Service.startService(self)

        try:
            # Returning a Deferred is not supported by startService(),
            # so instead attempt to acquire the lock synchonously!
            d = self.lock.acquire()
            if not d.called:
                d.cancel()
                service.Service.stopService(self)
                raise ResourceError(
                    "Failed to acquire CF Processor lock for service start"
                )

            try:
                self._startServiceWithLock()
            except Exception as e:
                raise ChannelFinderError(f"Failed to start service: {str(e)}") from e
            finally:
                self.lock.release()
        except Exception:
            service.Service.stopService(self)
            raise

    def _startServiceWithLock(self):
        """Initialize the service with acquired lock.

        Raises:
            ChannelFinderError: If unable to connect to or configure ChannelFinder
            ConfigurationError: If configuration is invalid
        """
        _log.info("CF_START")

        if self.client is None:  # For setting up mock test client
            try:
                self.client = ChannelFinderClient()

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
                    try:
                        self.client.set(property={"name": cf_property, "owner": owner})
                    except Exception as e:
                        raise ChannelFinderError(
                            f"Failed to set property {cf_property}: {str(e)}"
                        ) from e

                self.record_property_names_list = set(record_property_names_list)
                _log.debug(
                    "record_property_names_list = {}".format(
                        self.record_property_names_list
                    )
                )
            except ConnectionError as e:
                _log.exception("Cannot connect to Channelfinder service")
                raise ChannelFinderError(
                    "Cannot connect to Channelfinder service"
                ) from e
            except Exception as e:
                raise ChannelFinderError(f"Error initializing service: {str(e)}") from e
            else:
                if self.conf.getboolean("cleanOnStart", True):
                    try:
                        self.clean_service()
                    except Exception as e:
                        _log.error(f"Failed to clean service on start: {str(e)}")
                        # Don't raise here as this is not critical for service start

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
                    property = new_property(
                        cf_prop_name,
                        owner,
                        transaction.client_infos.get(epics_env_var_name),
                    )
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
        records_infos: dict[str, list[Property]] = {}
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
                    property = new_property(
                        infotag,
                        owner,
                        record_infos_to_add[infotag],
                    )
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
            property=new_property("pvStatus", owner, "Inactive"),
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
                update_records_down_ioc(
                    recordInfoByName,
                    owner,
                    iocTime,
                    records_dict,
                    iocs,
                    conf,
                    recceiverid,
                    records,
                    cf_record,
                )
            else:
                if cf_record["name"] in new_records:  # case: record in old and new
                    update_record_old_and_new(
                        recordInfoByName,
                        owner,
                        iocTime,
                        conf,
                        new_records,
                        records,
                        old_records,
                        cf_record,
                    )
    # now pvNames contains a list of pv's new on this host/ioc
    """A dictionary representing the current channelfinder information associated with the pvNames"""
    existingChannels = {}

    update_get_existing_channels(processor, client, conf, new_records, existingChannels)

    update_handle_new_records(
        recordInfoByName,
        hostName,
        iocName,
        iocIP,
        iocid,
        owner,
        iocTime,
        conf,
        recceiverid,
        new_records,
        records,
        existingChannels,
    )
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


def update_handle_new_records(
    recordInfoByName: Dict[str, Dict[str, Any]],
    hostName: str,
    iocName: str,
    iocIP: str,
    iocid: str,
    owner: str,
    iocTime: str,
    conf: ConfigAdapter,
    recceiverid: str,
    new_records: Set[str],
    records: List[Dict[str, Any]],
    existingChannels: Dict[str, Dict[str, Any]],
) -> None:
    """Handle processing of new records"""
    for record_name in new_records:
        newProps = create_properties(
            owner, iocTime, recceiverid, hostName, iocName, iocIP, iocid
        )
        if conf.get("recordType", "default") == "on":
            newProps.append(
                recordType_property(
                    owner,
                    recordInfoByName[record_name]["recordType"],
                )
            )
        if (
            record_name in recordInfoByName
            and "infoProperties" in recordInfoByName[record_name]
        ):
            newProps = newProps + recordInfoByName[record_name]["infoProperties"]

        if record_name in existingChannels:
            update_existing_record_different_hostname(
                recordInfoByName,
                owner,
                conf,
                records,
                existingChannels,
                record_name,
                newProps,
            )
        else:
            update_handle_new_record(
                recordInfoByName, owner, conf, records, record_name, newProps
            )


def update_handle_new_record(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    conf: ConfigAdapter,
    records: List[Dict[str, Any]],
    record_name: str,
    newProps: List[Property],
) -> None:
    """Process a new record entry"""
    records.append({"name": record_name, "owner": owner, "properties": newProps})
    _log.debug("Add new record: {s}".format(s=records[-1]))
    if conf.get("alias", "default") == "on":
        if (
            record_name in recordInfoByName
            and "aliases" in recordInfoByName[record_name]
        ):
            alProps = [new_property("alias", owner, record_name)]
            for p in newProps:
                alProps.append(p)
            for alias in recordInfoByName[record_name]["aliases"]:
                records.append({"name": alias, "owner": owner, "properties": alProps})
                _log.debug("Add new alias: {s}".format(s=records[-1]))


def update_existing_record_different_hostname(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    conf: ConfigAdapter,
    records: List[Dict[str, Any]],
    existingChannels: Dict[str, Dict[str, Any]],
    record_name: str,
    newProps: List[Property],
) -> None:
    """Update existing record with different hostname/iocname"""
    existingChannel = existingChannels[record_name]
    existingChannel["properties"] = __merge_property_lists(
        newProps, existingChannel["properties"]
    )
    records.append(existingChannel)
    _log.debug("Add existing record with different IOC: {s}".format(s=records[-1]))

    if conf.get("alias", "default") == "on":
        if (
            record_name in recordInfoByName
            and "aliases" in recordInfoByName[record_name]
        ):
            alProps = [alias_property(owner, record_name)]
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
                    "Add existing alias with different IOC: {s}".format(s=records[-1])
                )


def update_get_existing_channels(
    processor: CFProcessor,
    client: ChannelFinderClient,
    conf: ConfigAdapter,
    new_records: Set[str],
    existingChannels: Dict[str, Dict[str, Any]],
) -> None:
    """Get existing channels with URL length limitation handling"""
    searchStrings: List[str] = []
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


def update_record_old_and_new(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    iocTime: str,
    conf: ConfigAdapter,
    new_records: Set[str],
    records: List[Dict[str, Any]],
    old_records: List[Dict[str, Any]],
    cf_record: Dict[str, Any],
) -> None:
    """Update record that exists in both old and new records"""
    cf_record["properties"] = __merge_property_lists(
        [
            active_property(owner),
            time_property(owner, iocTime),
        ],
        cf_record["properties"],
    )
    records.append(cf_record)
    _log.debug("Add existing record with same IOC: {s}".format(s=records[-1]))
    new_records.remove(cf_record["name"])

    if conf.get("alias", "default") == "on":
        if (
            cf_record["name"] in recordInfoByName
            and "aliases" in recordInfoByName[cf_record["name"]]
        ):
            for alias in recordInfoByName[cf_record["name"]]["aliases"]:
                if alias in old_records:
                    alias["properties"] = __merge_property_lists(
                        [
                            active_property(owner),
                            time_property(owner, iocTime),
                        ],
                        alias["properties"],
                    )
                    records.append(alias)
                    new_records.remove(alias["name"])
                else:
                    aprops = __merge_property_lists(
                        [
                            active_property(owner),
                            time_property(owner, iocTime),
                            alias_property(owner, cf_record["name"]),
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
                    "Add existing alias with same IOC: {s}".format(s=records[-1])
                )


def update_records_down_ioc(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    iocTime: str,
    records_dict: Dict[str, List[str]],
    iocs: Dict[str, Dict[str, Any]],
    conf: ConfigAdapter,
    recceiverid: str,
    records: List[Dict[str, Any]],
    cf_record: Dict[str, Any],
) -> None:
    """Update records when IOC is down"""
    if cf_record["name"] in records_dict:
        update_record_ioc_properties(
            owner, iocTime, records_dict, iocs, conf, recceiverid, cf_record
        )
        records.append(cf_record)
        _log.debug("Add existing record to previous IOC: {s}".format(s=records[-1]))

        if conf.get("alias"):
            update_records_delete_alias(
                recordInfoByName,
                owner,
                iocTime,
                records_dict,
                iocs,
                conf,
                recceiverid,
                records,
                cf_record,
            )
    else:
        cf_record["properties"] = __merge_property_lists(
            [
                inactive_property(owner),
                time_property(owner, iocTime),
            ],
            cf_record["properties"],
        )
        records.append(cf_record)
        _log.debug("Add orphaned record with no IOC: {s}".format(s=records[-1]))

        if conf.get("alias", "default") == "on":
            update_records_orphan_alias(
                recordInfoByName, owner, iocTime, records, cf_record
            )


def update_records_orphan_alias(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    iocTime: str,
    records: List[Dict[str, Any]],
    cf_record: Dict[str, Any],
) -> None:
    """Update orphaned aliases"""
    if (
        cf_record["name"] in recordInfoByName
        and "aliases" in recordInfoByName[cf_record["name"]]
    ):
        for alias in recordInfoByName[cf_record["name"]]["aliases"]:
            alias["properties"] = __merge_property_lists(
                [
                    inactive_property(owner),
                    time_property(owner, iocTime),
                ],
                alias["properties"],
            )
            records.append(alias)
            _log.debug("Add orphaned alias with no IOC: {s}".format(s=records[-1]))


def update_record_ioc_properties(
    owner: str,
    iocTime: str,
    records_dict: Dict[str, List[str]],
    iocs: Dict[str, Dict[str, Any]],
    conf: ConfigAdapter,
    recceiverid: str,
    cf_record: Dict[str, Any],
) -> None:
    """Update IOC-related properties for a channel finder record.

    This function updates the properties of a channel finder record with information
    from its associated IOC. It handles owner assignment, property merging, and
    record type updates.

    Args:
        owner: Owner of the record properties
        iocTime: Timestamp for the record update
        records_dict: Dictionary mapping record names to list of IOC IDs
        iocs: Dictionary containing IOC information
        conf: Configuration adapter
        recceiverid: Unique identifier for the receiver
        cf_record: Channel finder record to update

    Returns:
        None

    Note:
        The function modifies the cf_record dictionary in-place by updating its
        properties and owner information.
    """
    cf_record["owner"] = iocs[records_dict[cf_record["name"]][-1]]["owner"]
    cf_record["properties"] = __merge_property_lists(
        ch_create_properties(
            owner, iocTime, recceiverid, records_dict, iocs, cf_record
        ),
        cf_record["properties"],
    )
    if conf.get("recordType"):
        cf_record["properties"] = __merge_property_lists(
            cf_record["properties"].append(
                recordType_property(
                    owner,
                    str(iocs[records_dict[cf_record["name"]][-1]]["recordType"]),
                )
            ),
            cf_record["properties"],
        )


def update_records_delete_alias(
    recordInfoByName: Dict[str, Dict[str, Any]],
    owner: str,
    iocTime: str,
    records_dict: Dict[str, List[str]],
    iocs: Dict[str, Dict[str, Any]],
    conf: ConfigAdapter,
    recceiverid: str,
    records: List[Dict[str, Any]],
    cf_record: Dict[str, Any],
) -> None:
    """Update and delete aliases for a record when IOC is down.

    This function processes aliases associated with a channel finder record when its IOC
    is down. It updates the properties of existing aliases and handles their deletion
    if necessary.

    Args:
        recordInfoByName: Dictionary mapping record names to their detailed information
        owner: Owner of the record properties
        iocTime: Timestamp for the record update
        records_dict: Dictionary mapping record names to list of IOC IDs
        iocs: Dictionary containing IOC information including hostname, name, etc.
        conf: Configuration adapter for accessing settings
        recceiverid: Unique identifier for the receiver
        records: List of records to be updated
        cf_record: Channel finder record being processed

    Returns:
        None

    Note:
        The function modifies the records list in-place by appending updated aliases.
        It also updates properties of existing aliases with new IOC information.
    """
    if (
        cf_record["name"] in recordInfoByName
        and "aliases" in recordInfoByName[cf_record["name"]]
    ):
        for alias in recordInfoByName[cf_record["name"]]["aliases"]:
            if alias["name"] in records_dict:
                alias["owner"] = iocs[records_dict[alias["name"]][-1]]["owner"]
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
                if conf.get("recordType"):
                    cf_record["properties"] = __merge_property_lists(
                        cf_record["properties"].append(
                            recordType_property(
                                owner,
                                str(
                                    iocs[records_dict[alias["name"]][-1]]["recordType"]
                                ),
                            )
                        ),
                        cf_record["properties"],
                    )
                records.append(alias)
                _log.debug(
                    "Add existing alias to previous IOC: {s}".format(s=records[-1])
                )


def new_property(name: str, owner: str, value: str) -> Property:
    """Create a new property dictionary with the specified attributes.

    This function creates a standardized property dictionary used throughout
    the channel finder service.

    Args:
        name: Name of the property
        owner: Owner of the property
        value: Value associated with the property

    Returns:
        Property: A dictionary containing name, owner, and value keys

    Example:
        >>> prop = new_property("status", "admin", "active")
        >>> print(prop)
        {'name': 'status', 'owner': 'admin', 'value': 'active'}
    """
    return {"name": name, "owner": owner, "value": value}


def alias_property(owner: str, value: str) -> Property:
    """Create a new alias property.

    This function creates a property dictionary specifically for aliases,
    using the standard property structure.

    Args:
        owner: Owner of the alias property
        value: Value of the alias (typically the original record name)

    Returns:
        Property: A dictionary containing the alias property information

    Example:
        >>> prop = alias_property("admin", "original_record_name")
        >>> print(prop)
        {'name': 'alias', 'owner': 'admin', 'value': 'original_record_name'}
    """
    return new_property("alias", owner, value)


def pvStatus_property(owner: str, value: str) -> Property:
    """Create a new PV status property.

    This function creates a property dictionary specifically for PV status,
    using the standard property structure.

    Args:
        owner: Owner of the status property
        value: Status value (typically 'Active' or 'Inactive')

    Returns:
        Property: A dictionary containing the PV status property information

    Example:
        >>> prop = pvStatus_property("admin", "Active")
        >>> print(prop)
        {'name': 'pvStatus', 'owner': 'admin', 'value': 'Active'}
    """
    return new_property("pvStatus", owner, value)


def active_property(owner: str) -> Property:
    """Create a property indicating an active PV status.

    This is a convenience function that creates a PV status property
    with the value 'Active'.

    Args:
        owner: Owner of the status property

    Returns:
        Property: A dictionary containing the active status property

    Example:
        >>> prop = active_property("admin")
        >>> print(prop)
        {'name': 'pvStatus', 'owner': 'admin', 'value': 'Active'}
    """
    return pvStatus_property(owner, "Active")


def inactive_property(owner: str) -> Property:
    """Create a property indicating an inactive PV status.

    This is a convenience function that creates a PV status property
    with the value 'Inactive'.

    Args:
        owner: Owner of the status property

    Returns:
        Property: A dictionary containing the inactive status property

    Example:
        >>> prop = inactive_property("admin")
        >>> print(prop)
        {'name': 'pvStatus', 'owner': 'admin', 'value': 'Inactive'}
    """
    return pvStatus_property(owner, "Inactive")


def recordType_property(owner: str, value: str) -> Property:
    """Create a property specifying the record type.

    This function creates a property dictionary specifically for record types,
    using the standard property structure.

    Args:
        owner: Owner of the record type property
        value: The record type value

    Returns:
        Property: A dictionary containing the record type property information

    Example:
        >>> prop = recordType_property("admin", "ai")
        >>> print(prop)
        {'name': 'recordType', 'owner': 'admin', 'value': 'ai'}
    """
    return new_property("recordType", owner, value)


def time_property(owner: str, value: str) -> Property:
    """Create a property for timestamp information.

    This function creates a property dictionary specifically for timestamps,
    using the standard property structure.

    Args:
        owner: Owner of the time property
        value: The timestamp value

    Returns:
        Property: A dictionary containing the time property information

    Example:
        >>> prop = time_property("admin", "2024-03-14T12:00:00")
        >>> print(prop)
        {'name': 'time', 'owner': 'admin', 'value': '2024-03-14T12:00:00'}
    """
    return new_property("time", owner, value)


def create_properties(
    owner: str,
    iocTime: str,
    recceiverid: str,
    hostName: str,
    iocName: str,
    iocIP: str,
    iocid: str,
) -> List[Property]:
    """Create a standard set of properties for a channel finder record.

    This function creates a list of essential properties that every channel finder
    record should have, including host information, IOC details, and status.

    Args:
        owner: Owner of the properties
        iocTime: Timestamp for the record
        recceiverid: Unique identifier for the receiver
        hostName: Name of the host machine
        iocName: Name of the IOC
        iocIP: IP address of the IOC
        iocid: Unique identifier for the IOC

    Returns:
        List[Property]: A list of property dictionaries containing all standard properties

    Note:
        The returned properties include: hostName, iocName, iocid, iocIP, pvStatus,
        time, and recceiverid.
    """
    return [
        new_property("hostName", owner, hostName),
        new_property("iocName", owner, iocName),
        new_property("iocid", owner, iocid),
        new_property("iocIP", owner, iocIP),
        active_property(owner),
        time_property(owner, iocTime),
        new_property(RECCEIVERID_KEY, owner, recceiverid),
    ]


def ch_create_properties(
    owner: str,
    iocTime: str,
    recceiverid: str,
    records_dict: Dict[str, List[str]],
    iocs: Dict[str, Dict[str, Any]],
    cf_record: Dict[str, Any],
) -> List[Property]:
    """Create channel-specific properties using IOC information.

    This function creates a list of properties for a specific channel by looking up
    IOC information from the records dictionary and IOCs dictionary.

    Args:
        owner: Owner of the properties
        iocTime: Timestamp for the record
        recceiverid: Unique identifier for the receiver
        records_dict: Dictionary mapping record names to list of IOC IDs
        iocs: Dictionary containing IOC information
        cf_record: Channel finder record being processed

    Returns:
        List[Property]: A list of property dictionaries for the channel

    Note:
        This function uses the last IOC in the records_dict list for the record
        to determine the property values.
    """
    return create_properties(
        owner,
        iocTime,
        recceiverid,
        iocs[records_dict[cf_record["name"]][-1]]["hostname"],
        iocs[records_dict[cf_record["name"]][-1]]["iocname"],
        iocs[records_dict[cf_record["name"]][-1]]["iocIP"],
        records_dict[cf_record["name"]][-1],
    )


def __merge_property_lists(
    newProperties: List[Property],
    oldProperties: List[Property],
) -> List[Property]:
    """Merge two property lists ensuring no duplicates.

    This function combines two lists of properties while ensuring that no duplicate
    property names exist in the final list. Properties from newProperties take
    precedence over those in oldProperties.

    Args:
        newProperties: List of new properties to add
        oldProperties: List of existing properties

    Returns:
        List[Property]: A merged list of properties with no duplicates

    Example:
        >>> new_props = [{'name': 'status', 'owner': 'admin', 'value': 'active'}]
        >>> old_props = [{'name': 'type', 'owner': 'admin', 'value': 'ai'}]
        >>> merged = __merge_property_lists(new_props, old_props)
        >>> print(len(merged))  # Will be 2 as properties have different names
    """
    newPropNames = [p["name"] for p in newProperties]
    for oldProperty in oldProperties:
        if oldProperty["name"] not in newPropNames:
            newProperties = newProperties + [oldProperty]
    return newProperties


def getCurrentTime(timezone: bool = False) -> str:
    """Get current timestamp optionally with timezone information.

    This function returns the current time as a string. If timezone is True,
    it includes the timezone information in the returned string.

    Args:
        timezone: Whether to include timezone information in the output

    Returns:
        str: Current timestamp as string, optionally with timezone information

    Example:
        >>> print(getCurrentTime())  # '2024-03-14 12:00:00'
        >>> print(getCurrentTime(True))  # '2024-03-14 12:00:00+01:00'
    """
    if timezone:
        return str(datetime.datetime.now().astimezone())
    return str(datetime.datetime.now())


def prepareFindArgs(
    conf: ConfigAdapter, args: List[tuple[str, Any]], size: int = 0
) -> List[tuple[str, Any]]:
    """Prepare arguments for channel finder search.

    This function processes search arguments and applies size limits based on
    configuration. It modifies the search arguments to include size limitations
    if specified in the configuration.

    Args:
        conf: Configuration adapter for accessing settings
        args: List of search arguments as (key, value) tuples
        size: Default size limit for results (overridden by configuration)

    Returns:
        List[tuple[str, Any]]: Modified list of search arguments

    Note:
        If a size limit is specified in the configuration (findSizeLimit),
        it will be added to the arguments list with the '~size' key.
    """
    size_limit = int(conf.get("findSizeLimit", size))
    if size_limit > 0:
        args.append(("~size", size_limit))
    return args


def poll(
    update_method,
    processor: CFProcessor,
    recordInfoByName: dict[str, dict[str, Any]],
    records_to_delete: List[str],
    hostName: str,
    iocName: str,
    iocIP: str,
    iocid: str,
    owner: str,
    iocTime: str,
) -> bool:
    """Poll for updates with retry mechanism.

    This function attempts to update channel finder records with retry logic
    in case of failures. It implements an exponential backoff strategy for
    retries.

    Args:
        update_method: Function to call for updating records
        processor: CFProcessor instance managing the updates
        recordInfoByName: Dictionary of record information by name
        records_to_delete: List of records to be deleted
        hostName: Name of the host machine
        iocName: Name of the IOC
        iocIP: IP address of the IOC
        iocid: Unique identifier for the IOC
        owner: Owner of the records
        iocTime: Timestamp for the update

    Returns:
        bool: True if update was successful, False otherwise

    Note:
        The function implements exponential backoff with a maximum retry
        interval of 60 seconds. It will continue retrying indefinitely
        until successful or interrupted.
    """
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
    return False
