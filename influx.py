import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS
import os
import sys
import json

#https://docs.influxdata.com/influxdb3/cloud-serverless/write-data/best-practices/schema-design/

url = os.getenv("INFLUX_URL")
token = os.getenv("INFLUX_TOKEN")
org = os.getenv("INFLUX_ORG")
bucket = os.getenv("INFLUX_BUCKET")
pass_number = os.getenv("INFLUX_PASS_NUMBER")

if not url:
    print("Evnrionment variable INFLUX_URL is not set.")
    exit(-1)

if not token:
    print("Evnrionment variable INFLUX_TOKEN is not set.")
    exit(-2)

if not org:
    print("Evnrionment variable INFLUX_ORG is not set.")
    exit(-3)

if not bucket:
    print("Evnrionment variable INFLUX_BUCKET is not set.")
    exit(-4)

client = influxdb_client.InfluxDBClient(url=url, token=token, org=org)
write_api = client.write_api(write_options=SYNCHRONOUS)

try:
    for line in iter(sys.stdin.readline, b''):
        data = json.loads(line)
        for channel in data["SFDU"]:
            print(channel)
            p = influxdb_client.Point(f"{channel['name']}_{channel['measurement_id']}") \
                               .tag("abbreviation", channel['abbreviation']) \
                               .field("value", channel['value'])
            if pass_number:
                p.tag("pass_number", pass_number)
            try:
                write_api.write(bucket=bucket, org=org, record=p)
            except Exception as e:
                print(f"Error with point: {p}")
                print(e)
                sys.stdout.flush()
                client.close()
                exit(-1)

except KeyboardInterrupt:
    sys.stdout.flush()
    client.close()
    exit(-1)
    pass
