# You don't a DDD if your SCID is above 256, so skip that.
# Someone suggested that SFGI is available as TCP
# No warranty, no guarantees of correctness of data etc...

from bitstring import BitArray
from enum import Enum
import json
import xmltodict
from functools import reduce
import traceback as tb
import socket
import ast
import os
import time


def deep_get(dictionary, keys, default=None):
    return reduce(lambda d, key: d.get(key, default)
                  if isinstance(d, dict)
                  else default, keys.split("."), dictionary)


with open('./monitor_channel.xml', 'rb') as f:
    q = xmltodict.parse(f)


enum_table = {}
for i in deep_get(q, 'telemetry_dictionary.enum_definitions.enum_table'):
    d = {}
    for j in deep_get(i, 'values.enum'):
        if isinstance(j, str):
            j = {'@symbol': 'None', '@numeric': '0'}
        d[j['@symbol']] = int(j['@numeric'])
        e = Enum(i['@name'], d)
        enum_table[e.__name__] = e


class Monitor_Channel:
    def __init__(self, abbreviation, name, data_type, source,
                 byte_length, measurement_id,
                 title, description, categories):

        self.value = None
        self.abbreviation = abbreviation
        self.name = name
        self.data_type = data_type
        self.source = source
        self.byte_length = byte_length
        self.measurement_id = measurement_id
        self.title = title
        self.description = description
        self.categories = categories

    def __repr__(self):
        return str(self.__dict__)

    def decode(self, val_bytes):
        # TODO: This is clunky
        if self.data_type == 'string':
            if isinstance(val_bytes, int):
                # val_bytes was extracted as an integer and not bytes (lv_flag=0)
                # See channel 1099, 1246, etc...
                # integer values acually represent an ordinal character (e.g. X=88)
                self.value = chr(val_bytes)
            else:
                # It's actually a string
                val_bytes = val_bytes.bytes
                # There's some null bytes in here, we left a note in the CHDO extraction
                self.value = val_bytes.decode('ASCII').replace("\x00", "")
        elif self.data_type == 'enum':
            if not isinstance(val_bytes, int):
                val_bytes = val_bytes.bytes
                i = int.from_bytes(val_bytes, 'big', signed=False)
            else:
                i = val_bytes
            enum_type = enum_table[f"Enum_{self.abbreviation}"]
            self.value = enum_type(i).name
        elif self.data_type == 'float':
            if isinstance(val_bytes, int):
                self.value = float(val_bytes)
            else:
                self.value = val_bytes.f
        elif self.data_type == 'integer':
            self.value = val_bytes.int
        elif self.data_type == 'unsigned':
            if isinstance(val_bytes, int):
                self.value = val_bytes
            else:
                self.value = int(val_bytes.uint)
        elif os.getenv("DEBUG"):
            print(f"Unknown type: {self.data_type}")

        return self.value

    def canonical_map(self):
        try:
            d = {}
            d['abbreviation'] = self.abbreviation
            d['measurement_id'] = self.measurement_id
            d['title'] = self.title
            d['name'] = self.name
            d['description'] = self.description
            d['source'] = self.source
            d['categories'] = self.categories
            d['byte_length'] = self.byte_length
            d['data_type'] = self.data_type
            d['value'] = self.value
        except Exception as e:
            print(e)
            print(self.name)
            return {}

        return d


channel_types = {}
for i in deep_get(q, 'telemetry_dictionary.telemetry_definitions.telemetry'):
    m_id = i.get('measurement_id', None)
    if not m_id:
        continue
    measurement_id = int(m_id)
    byte_length = int(i['@byte_length'])
    d = deep_get(i, 'categories.category')
    m = {}
    for p in d:
        k = p['@name']
        v = p['@value']
        m[k] = v

    categories = m
    channel_types[measurement_id] = Monitor_Channel(i['@abbreviation'],
                                                    i['@name'],
                                                    i['@type'],
                                                    i['@source'],
                                                    byte_length,
                                                    measurement_id,
                                                    i['title'],
                                                    i['description'].strip("'"),
                                                    categories)

"""
A plugin which creates an RAF connection with the DSN.
Frames received via the RAF connection are sent to the output stream
"""


class DSN_Monitor():
    def __init__(self):
        pass

    def __call__(self, data):
        return self.process_sdb(data)

    def process_sdb(self, sdb):
        chunks = {'DDD_HEADER': sdb[0:20],
                  'SFDU': sdb[20:-2],
                  'DDD_TRAILER': sdb[-2:None]}

        ddd_header = self.process_ddd_header(chunks["DDD_HEADER"])
        sfdu = self.process_sfdu(chunks['SFDU'])

        res = {'DDD_HEADER': ddd_header,
               "SFDU": sfdu
               }

        return res

    def process_ddd_header(self, sdd): # OK
        # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=1705

        def process_destination_assembly(chunk):
            res = {}
            res['reserved_1'] = int(chunk[0])

            # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2672
            # I don't want to publish this table, don't decode it.
            res['facility_code'] = chunk[1:8].uint
            res['subfacility_code'] = chunk[8:12].uint

            # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=1702
            res['assembly_code'] = chunk[12:15].uint
            res['reserved_2'] = int(chunk[15])
            return res

        def process_source_assembly(chunk):
            return process_destination_assembly(chunk)

        def process_stream_description(chunk):
            res = {}

            # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2687
            res['spacecraft_code'] = chunk[0:8].uint

            # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=1705
            res['data_type'] = chunk[8:15].uint

            res['data_nature'] = int(chunk[15])

            if res['data_nature']:
                res['data_nature'] = 'playback_data'
            else:
                res['data_nature'] = 'real-time data'

            # This is a hack to get around writing a table
            if res['data_type'] == 112:
                res['data_type'] = '0158-Monitor Data'
            else:
                res['data_type'] = f"Unknown data type: + {res['data_type']}"
            return res

        def decode_protocol(chunk):
            protocol = chunk.uint
            if protocol == 1:
                protocol = "DDD Protocol (pure SDB)"
            elif protocol == 2:
                protocol = "OPS-6-7"
            elif protocol == 3:
                protocol = "OPS-6-8"
            elif protocol == 4:
                protocol = "DFMC Protocol"
            else:
                protocol = "Error: Unknown Protocol"
            return protocol

        def decode_doy(chunk):
            # BCD
            hundreds = chunk[0:2].uint * 100
            tens = chunk[2:5].uint * 10
            units = chunk[5:10].uint  # * 1
            return hundreds+tens+units

        def decode_year(chunk):
            thousands = chunk[0:4].uint * 1000
            hundreds = chunk[3:6].uint * 100
            tens = chunk[6:12].uint * 10
            units = chunk[12:16].uint  # * 1
            return thousands+hundreds+tens+units

        def decode_grade_of_service(chunk):
            res = {}

            precedence = chunk[0:3]
            if precedence == "0b000":
                precedence = "Routine"
            elif precedence == "0b001":
                precedence = "Priority"
            elif precedence == "0b010":
                precedence = "Immediate"
            elif precedence == "0b011":
                precedence = "Flash"
            elif precedence == "0b100":
                precedence = "Flash Override"
            elif precedence == "0b101":
                precedence = "CRITIC/ECP"
            elif precedence == "0b110":
                precedence = "Internetwork Control"
            elif precedence == "0b111":
                precedence = "Network Control"
            else:
                precedence = "Unknown Precedence"
            res["precedence_level"] = precedence

            delay = chunk[4]
            if delay:
                delay = "Low Delay"
            else:
                delay = "Normal Delay"
            res['delay'] = delay

            res['throughput'] = int(chunk[5])

            reliability = chunk[6]
            if reliability:
                reliability = "High Reliablility"
            else:
                reliability = "Normal Reliability"
            res['reliability'] = reliability

            res['reserved_1'] = str(chunk[6:9])
            return res

        def decode_priority(chunk):
            # There's an error in the ICD, this actually exists in another table.
            res = {}

            res['reserved_1'] = int(chunk[0])
            if chunk[1:3]:
                delay = "low"
            else:
                delay = "normal"
            res['delay'] = delay

            if chunk[3:4]:
                reliability = "high"
            else:
                reliability = "normal"
            res['reliability'] = reliability

            res["reserved_2"] = str(chunk[4:8])
            return res

        sdd = BitArray(sdd)
        res = {}
        res['destination_assembly'] = process_destination_assembly(sdd[0:16])
        res['source_assembly'] = process_source_assembly(sdd[16:32])
        res['stream_description'] = process_stream_description(sdd[32:48])
        res['total_length'] = sdd[48:64].uint #octets: sdb + header + data + trailer
        res['block_serial_number'] = sdd[64:80].uint #seq mod 65536
        res['protocol'] = decode_protocol(sdd[80:86])
        res['day_of_year'] = decode_doy(sdd[86:96])
        res['time'] = str(sdd[96:120]) # Centiseconds in UTC, it says its useful to leave it as bin
        res['virtual_stream_id'] = sdd[120:128].uint
        res['year'] = decode_year(sdd[128:144])
        res['gos'] = decode_grade_of_service(sdd[144:152])
        # This might be an error in the ICD, keeping it as reserved
        res['reserved'] = decode_priority(sdd[152:160])
        return res

    def process_sfdu(self, SFDU):
        # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2403

        # We're working in 16 bits here
        res = {}
        # OK
        SFDU_LABEL = SFDU[0:20]
        res['SFDU_LABEL'] = self.process_sfdu_label(SFDU_LABEL)

        AGGREGATION_CHDO = BitArray(SFDU[20:])
        res['AGGREGATION_CHDO'] = self.process_chdo_01(AGGREGATION_CHDO)

        PRIMARY_CHDO = res['AGGREGATION_CHDO']["CHDO_VALUE"]
        res['PRIMARY_CHDO'] = self.process_chdo_02(PRIMARY_CHDO)

        # Specialized Secondary Header for 0158
        SECONDARY_CHDO = BitArray(SFDU[32:54])
        res['SECONDARY_CHDO'] = self.process_chdo_73(SECONDARY_CHDO)

        TERTIARY_CHDO = BitArray(SFDU[54:58])
        res['TERTIARY_CHDO'] = self.process_chdo_000(TERTIARY_CHDO)

        QUATERNARY_CHDO = BitArray(SFDU[58:68])
        res['QUATERNARY_CHDO'] = self.process_chdo_27(QUATERNARY_CHDO)

        chd0_index = res['AGGREGATION_CHDO']['CHDO_LENGTH'] + 24
        DATA_CHDO = BitArray(SFDU[chd0_index:])
        res['DATA_CHDO'] = self.process_chdo_28(DATA_CHDO, res['QUATERNARY_CHDO']['NUMBER_CHANNELS'])

        final = []
        for channel in res['DATA_CHDO']:
            if channel['lc_value']:
                val = channel['lc_value']
            else:
                val = channel['length_value']
            c = channel_types.get(channel['channel_number'], None)
            if c:
                try:
                    # Wondering if we need to decode length_value
                    c.decode(val)
                    final.append(c.canonical_map())
                except Exception as e:
                    tb.print_exc()
                    return final
            else:
                print(f"M-{channel['channel_number']} is not in the dictionary.")
        return final

    def process_chdo_28(self, chunk, num_channels):
        # Channelized Data Area
        # https://jaguar.jpl.nasa.gov/SW_detail.php
        data_qual = {}
        data_qual[0] = "No error, not filler."
        data_qual[1] = "The decom map tried to make a channel, but the record had no data at that location."
        data_qual[2] = "Filler data was decommutated."
        data_qual[3] = "Stale"

        res = {}
        res["CHDO_TYPE"] = chunk[0:16].uint # Fixed to 28
        res["CHDO_LENGTH"] = chunk[16:32].uint
        # This is fixed for the entire SFDU

        # These are the channels
        extracted_channels = []
        dat = chunk[32:]
        for i in range(0, num_channels):
            res = {}
            res['source'] = chr(ord('@')+dat[0:5].uint) # Fixed to 13=M, corresponding to 0158 ICD M-nnnn code
            res['lv_flag'] = dat[5]

            res['data_qual'] = dat[6:8].uint
            res['data_qual'] = data_qual.get(res['data_qual'], "Unknown")

            length_value = dat[8:16]
            res['filler_length'] = dat[16:19].uint
            res['channel_number'] = dat[19:32].uint

            if res['lv_flag']:
                # Trim filler on the left
                # We're not going to convert lenngth_value since we don't know how it should be interpreted.
                # We'll let the AMPCS dict handle it, which lets us treat everything uniformly.
                # But we'll fix this next time.
                length_value = length_value[res['filler_length']:]
                res['length_value'] = length_value.uint
                res['lc_value'] = None
                dat = dat[32:]
            else:
                res['length_value'] = length_value.uint
                consume = res['length_value']*16
                res['lc_value'] = dat[32:32+consume]
                # Trim filler on the left
                # We'll fix this with the fix for length_value
                # the byte alignment doesn't work out when we get to the AMPCS dict conversion
                # res['lc_value'] = res['lc_value'][res['filler_length']:]
                dat = dat[32+consume:]

            extracted_channels.append(res)
        # We should have len(dat) == 0 by here, and we're done with extraction
        return extracted_channels

    def process_chdo_27(self, dat): # OK
        # Quarternary
        # 80 bits
        dat = dat.bytes
        res = {}
        res["CHDO_TYPE"] = dat[0:2]
        res["CHDO_LENGTH"] = dat[2:4]
        res["DECOM_FLAGS"] = dat[5:6]

        res["FILLER_LENGTH"] = dat[5:6]
        res["NUMBER_CHANNELS"] = dat[6:8]

        res = {k: int.from_bytes(v, 'big', signed=False)
               for (k, v) in res.items()}

        res["MAP_ID"] = dat[8:10].hex() # Fixed to FFFF if not set (i.e. version number for decom)
        res["DECOM_FLAGS"] = None # 0158 wants us to ignore these
        return res

    def process_chdo_000(self, dat): # OK
        # Tertiary NULL
        # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2403
        # 16 bits of NOTHING!
        dat = dat.bytes
        res = {}
        res['CHDO_TYPE'] = dat[0:2] # Fixed 0
        res['CHDO_LENGTH'] = dat[2:4] # Fixed 0
        res = {k: int.from_bytes(v, 'big', signed=False)
               for (k, v) in res.items()}
        return res

    def process_chdo_73(self, dat): # OK
        # Secondary (Special for 0158 Station Monitor Data)
        # Document 820-013, Module 0172-Telecomm-002, Rev. -1; Don't trust it, 0158 overrides many fields
        # See https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2530
        # Document 820-013, Module 0158-Monitor, Rev. K
        dat = dat.bytes
        res = {}
        res["CHDO_TYPE"] = dat[0:2] # Fixed to 78 if telemetry, 73 for 0158 monitor secondary
        res["CHDO_LENGTH"] = dat[2:4]
        res["ORIGINATOR"] = dat[4:5] # Fixed to 48 (e.g. DSN)
        res["LAST_MODIFIER"] = dat[5:6] # Fixed to 48 (e.g. DSN)
        res["8B_SCFT_ID"] = dat[6:7] # Fixed to 0 if SCID is over 256, see SCFT_ID field
        res["DATA_SOURCE"] = dat[7:8] # Seems to correspond to antenna/facility somewhere
        res['SPARE_1'] = dat[8:10] # Fixed to 0
        # This corresponds to Explorer-1 epoch time (01 Jan 1958) in seconds and ms, which is RAD.
        # I don't want to deal with the conversion, so we'll leave it as is and interpret it as unix time + epoch offset
        res["MST"] = dat[10:16]
        res["SOURCE"] = dat[16:17] # https://dsnprocess.jpl.nasa.gov/dsirt/ (i.e. the antenna)
        res["SPARE_2"] = dat[17:18] # Fixed 0
        res["SCFT_ID"] = dat[18:20] # Matches 8b_scft field if it is not 0 (e.g. < 256)
        res["SPARE_3"] = dat[20:22] # Fixed to 0

        res = {k: int.from_bytes(v, 'big', signed=False)
               for (k, v) in res.items()}
        return res

    def process_chdo_02(self, dat): # OK
        # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2403
        # Primary
        # 8 bytes
        dat = dat.bytes
        res = {}
        res['CHDO_TYPE'] = int.from_bytes(dat[0:2], 'big', signed=False)
        res['CHDO_LENGTH'] = int.from_bytes(dat[2:4], 'big', signed=False)
        RECORD_ID = dat[4:8]
        res['MAJOR'] = RECORD_ID[0] # Fixed to 11 0158
        res['MINOR'] = RECORD_ID[1] # Fixed to 2 0158
        res['MISSION'] = RECORD_ID[2] # See https://jaguar.jpl.nasa.gov/SW_detail.php?modid=2491, they call this the SFDU Mission ID in an excel sheet with the SCID and Mission IDs.
        res['FORMAT'] = RECORD_ID[3] # Fixed to 5 in 0158
        return res

    def process_chdo_01(self, dat): # Might be ok
        # Aggregation
        # 32 bits + extra
        res = {}
        res["CHDO_TYPE"] = dat[0:16].uint
        # Length of the rest of the CHDOs aggregated except for the data CHDO
        res["CHDO_LENGTH"] = dat[16:32].uint
        # There might be other CHDOs before the data CHDO e.g. the aggregates except for the data CHDO (primary, secondary, tertiary, quaternary)
        res["CHDO_VALUE"] = dat[32:res["CHDO_LENGTH"]*8]
        return res

    def process_sfdu_label(self, chunk):# OK
        # https://jaguar.jpl.nasa.gov/SW_detail.php?modid=1071
        # Also see 0158 ICD
        chunk = BitArray(chunk)
        res = {}
        # The ICD is goofy and splits the bitcount into 4 bytes_16, it's 32 bits.
        res["CONTROL_AUTHORITY"] = chunk[0:32].bytes.decode('ASCII')
        res["VERSION_ID"] = chunk[32:40].bytes.decode("ASCII")
        res["CLASS_ID"] = chunk[40:48].bytes.decode("ASCII")
        res["SPARE"] = chunk[48:64].bytes.decode("ASCII")
        # 0158 ICD calls this ddp_id
        # 0158 and SFDU ICDs do not agree on the constant value
        res["DATA_DESCRIPTION_ID"] = chunk[64:96].bytes.decode("ASCII")
        # 0158 ICD calls this block_length, max is 4480 bytes in 0158 SFDU
        res['LENGTH_ATTRIBUTE'] = chunk[96:320].uint
        return res


def main():
    processor = DSN_Monitor()
    if os.getenv("TEST_NDJSON"):
        with open('./station_monitor_data.ndjson', 'r') as f:
            while x := f.readline():
                x = json.loads(x)
                x = ast.literal_eval(x)
                res = processor(x)
                print(json.dumps(res))
                time.sleep(.2) # 5 Hz
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = os.getenv("PORT", 8001)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.getenv("DEBUG"):
            print.info(f'Starting station monitor server on: {host}:{port}')
        s.bind((host, port))

        while True:
            data, _ = s.recvfrom(60000) # 4500 + 20 + 2 bytes max
            res = processor.process(data)
            print(json.dumps(res))


if __name__ == "__main__":
    main()


