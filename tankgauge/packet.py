"""Combined UDP packet layout shared with the CODESYS PLC (float32 LE, column-major)."""

from __future__ import annotations

import struct
from collections.abc import Sequence

import pandas as pd

REAL_SIZE = 4               # One float32 (real), little-endian

NUM_COLUMNS = 3
NUM_HEADER_REALS = 8        # RTU_Number, Tank_Number + 6 FRAME_HEADER_FIELDS

# Soft cap inferred from PLC ARRAY[0..1360] OF REAL (1361 REALs). Used only for a warning.
PLC_BUFFER_REALS = 1361

COLUMN_LABELS = ('COL1', 'COL2', 'COL3')


def build_combined_packet(header_values: Sequence[tuple[str, float]], df: pd.DataFrame) -> bytes:
    """One UDP payload: 8 header REALs first, then CSV column-major.

    Layout (float32 LE):
        [0]                RTU_Number
        [1]                Tank_Number
        [2]                Column_Size
        [3]                Sensor_Min_bar
        [4]                Sensor_Max_bar
        [5]                Density
        [6]                Dead_Volume
        [7]                Tank_Height
        [8 .. 8+N-1]       COL1 (all rows)
        [8+N .. 8+2N-1]    COL2 (all rows)
        [8+2N .. 8+3N-1]   COL3 (all rows)
    """
    floats = [float(value) for _label, value in header_values]
    for col in range(NUM_COLUMNS):
        floats.extend(float(v) for v in df.iloc[:, col])
    return struct.pack(f'<{len(floats)}f', *floats)


def packet_format_summary() -> str:
    return (
        'UDP: ONE combined packet — 8 header REALs (indices 0–7) followed by '
        'CSV data (column-major float32 LE)'
    )


def describe_combined_packet(packet: bytes, n_rows: int | None = None, max_preview: int = 3) -> str:
    """Byte size, value preview, and hex for a combined header+CSV float32 packet."""
    size = len(packet)
    if size == 0 or size % REAL_SIZE != 0:
        return f'{size}B, format=invalid (not a multiple of 4B), hex={packet.hex(" ")}'
    n = size // REAL_SIZE
    vals = struct.unpack(f'<{n}f', packet)
    parts = []
    header = vals[:NUM_HEADER_REALS]
    parts.append('header[0..7]=' + ', '.join(str(v) for v in header))
    if n_rows is not None and NUM_HEADER_REALS + n_rows * NUM_COLUMNS == n:
        for col_i, label in enumerate(COLUMN_LABELS):
            start = NUM_HEADER_REALS + col_i * n_rows
            chunk = vals[start:start + n_rows]
            preview = ', '.join(str(v) for v in chunk[:max_preview])
            if n_rows > max_preview:
                preview += f', … ({n_rows} total)'
            parts.append(f'{label}[{start}..{start + n_rows - 1}]={preview}')
    layout = '; '.join(parts)
    hex_len = min(16, size)
    hex_part = packet[:hex_len].hex(' ')
    if size > hex_len:
        hex_part += ' …'
    return f'{size}B, format={n}×float32 LE, {layout}, hex={hex_part}'
