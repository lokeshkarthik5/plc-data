from app import TANK_OPTIONS
from tankgauge.tanks import TANKS, preset_header_fields


def test_twelve_tanks_numbered_in_order() -> None:
    assert [t.number for t in TANKS] == list(range(1, 13))
    assert {t.rtu for t in TANKS[:8]} == {4.0}
    assert {t.rtu for t in TANKS[8:]} == {5.0}


def test_fo1_preset() -> None:
    assert preset_header_fields(TANKS[0]) == {
        'sensor_min_bar': 0.0,
        'sensor_max_bar': 0.3,
        'density': 824.0,
        'dead_volume': 0.03,
        'tank_height': 2.03,
    }


def test_tank_options_derived_from_presets() -> None:
    assert [o['name'] for o in TANK_OPTIONS][:2] == ['1. FUEL OIL TANK 01', '2. FUEL OIL TANK 02']
    assert TANK_OPTIONS[11]['name'] == '12. DIRTY OIL TANK'
    assert all(o['tank'] == o['preset'].number for o in TANK_OPTIONS)
