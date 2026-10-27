from pathlib import Path

import pandas as pd
import pytest

from tankgauge.calibration import Point, load_points_csv

FIXTURES = Path(__file__).parent / 'fixtures'

FO1_HEADER = [
    ('RTU_Number', 4.0),
    ('Tank_Number', 1.0),
    ('Column_Size', 408.0),
    ('Sensor_Min_bar', 0.0),
    ('Sensor_Max_bar', 0.3),
    ('Density', 824.0),
    ('Dead_Volume', 0.03),
    ('Tank_Height', 2.03),
]


@pytest.fixture(scope='session')
def fo1_base() -> tuple[list[float], list[float], list[float]]:
    df = pd.read_csv(FIXTURES / 'fo1_base.csv')
    return df.iloc[:, 0].tolist(), df.iloc[:, 1].tolist(), df.iloc[:, 2].tolist()


@pytest.fixture(scope='session')
def fo1_points() -> list[Point]:
    return load_points_csv(str(FIXTURES / 'fo1_calibration.csv'))
