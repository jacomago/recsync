from channelfinder import ChannelFinderClient
import json
import os
from operator import itemgetter

filename = "/home/devuser/cfdata"  # change this to output file name
client = ChannelFinderClient()


def get_cf_data(client):
    records = client.findByArgs([("pvStatus", "Active")])

    for cf_record in records:
        cf_record.pop("owner", None)
        cf_record.pop("tags", None)
        for cf_property in cf_record["properties"]:
            if cf_property["name"] == "hostName":
                cf_record["hostName"] = cf_property["value"]
            if cf_property["name"] == "iocName":
                cf_record["iocName"] = cf_property["value"]
        cf_record.pop("properties", None)
    return records


records = get_cf_data(client)

if os.path.isfile(filename):
    os.remove(filename)

new_list = []

for record in records:
    new_list.append([record["name"], record["hostName"], int(record["iocName"])])

new_list.sort(key=itemgetter(0))

with open(filename, "x") as f:
    json.dump(new_list, f)
