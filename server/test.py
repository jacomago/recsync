import urllib.request
import json

import urllib3

def insert_fake_data(recc:str, cf: str, ch: str):
    http = urllib3.PoolManager()
    headers = urllib3.make_headers(basic_auth='admin:password')
    headers.update({'Content-Type': 
                                'application/json'})
    req = http.request('PUT', cf +"/ChannelFinder/resources/channels",
                       headers=headers,
                   body=json.dumps([
                       {
                           "name": ch,
                           "owner": recc,
                           "properties": [
                               {
                                   "name": "recceiverID",
                                   "owner": recc,
                                   "value": recc
                               },
                               {
                                   "name":"pvStatus",
                                   "owner": recc,
                                   "value": "Active"
                               }
                           ]
                       }
                   ]))


cf = "http://localhost:8080"
insert_fake_data("recc1", cf, "ch1" )
insert_fake_data("recc2", cf, "ch2" )
