import struct

import pandas as pd

from app import SAMPLE_CSV_ROWS
from tankgauge.packet import (
    NUM_HEADER_REALS,
    build_combined_packet,
    describe_combined_packet,
)

# Captured from app.py before packet.py was extracted (commit e525756).
SAMPLE_HEADER = [
    ('RTU_Number', 4.0),
    ('Tank_Number', 1.0),
    ('Column_Size', 3.0),
    ('Sensor_Min_bar', 0.0),
    ('Sensor_Max_bar', 0.3),
    ('Density', 824.0),
    ('Dead_Volume', 0.03),
    ('Tank_Height', 2.03),
]
SAMPLE_PACKET_HEX = (
    '000080400000803f00004040000000009a99993e00004e448fc2f53c85eb0140'
    '0000ca420000cc420000ce427b148e3f7b140e40b81e55405c8f1e41b81e0d41295cf740'
)


def test_sample_packet_bytes_unchanged() -> None:
    packet = build_combined_packet(SAMPLE_HEADER, pd.DataFrame(SAMPLE_CSV_ROWS))
    assert packet.hex() == SAMPLE_PACKET_HEX


def test_sample_packet_layout() -> None:
    packet = build_combined_packet(SAMPLE_HEADER, pd.DataFrame(SAMPLE_CSV_ROWS))
    vals = struct.unpack(f'<{len(packet) // 4}f', packet)
    assert len(vals) == NUM_HEADER_REALS + 3 * 3
    assert vals[NUM_HEADER_REALS:NUM_HEADER_REALS + 3] == (101.0, 102.0, 103.0)


def test_describe_combined_packet() -> None:
    packet = build_combined_packet(SAMPLE_HEADER, pd.DataFrame(SAMPLE_CSV_ROWS))
    text = describe_combined_packet(packet, n_rows=3)
    assert text.startswith('68B, format=17×float32 LE')
    assert 'COL1[8..10]=101.0, 102.0, 103.0' in text
    assert describe_combined_packet(b'\x00\x01').startswith('2B, format=invalid')
