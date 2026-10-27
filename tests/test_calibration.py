import struct
from pathlib import Path

import pandas as pd
import pytest
from conftest import FIXTURES, FO1_HEADER
from hypothesis import example, given
from hypothesis import strategies as st

from tankgauge.calibration import (
    CalibrationResult,
    Mode,
    Point,
    calibrate,
    clean_table,
    interp,
    load_tank_file,
    preview_reading,
    validate_calibration,
)
from tankgauge.packet import NUM_HEADER_REALS, PLC_BUFFER_REALS, build_combined_packet

TOL = 1e-4
Base = tuple[list[float], list[float], list[float]]


def run(base: Base, points: list[Point], mode: Mode) -> CalibrationResult:
    levels, volumes, weights = base
    return calibrate(levels, volumes, weights, points, mode, 824.0, tank_height=2.03)


def volume_at(result: CalibrationResult, level: float) -> float:
    return result.volumes[result.levels.index(level)]


def revised_at(result: CalibrationResult, level: float) -> float:
    i = result.levels.index(level)
    return result.volumes[i] + result.diffs[i]


def packet_for(result: CalibrationResult) -> bytes:
    df = pd.DataFrame({'COL1': result.levels, 'COL2': result.volumes, 'COL3': result.diffs})
    return build_combined_packet(FO1_HEADER, df)


# ---- interp ----

def test_interp_basic_and_endpoints() -> None:
    assert interp([0, 1, 2], [0, 10, 30], 0.5) == pytest.approx(5)
    assert interp([0, 1, 2], [0, 10, 30], 1) == pytest.approx(10)
    assert interp([0, 1, 2], [0, 10, 30], 2) == 30


def test_interp_out_of_range() -> None:
    with pytest.raises(ValueError):
        interp([0, 1], [0, 1], -0.01)
    with pytest.raises(ValueError):
        interp([0, 1], [0, 1], 1.01)


def test_interp_skips_zero_width_segment() -> None:
    assert interp([0, 1, 1, 2], [0, 10, 10, 30], 1) == pytest.approx(10)
    assert interp([0, 1, 2, 2], [0, 10, 30, 30], 2) == 30


# ---- clean_table ----

def test_clean_table_fo1(fo1_base: Base) -> None:
    levels, volumes, _ = fo1_base
    assert len(levels) == 408
    out_levels, out_volumes, warnings, errors = clean_table(levels, volumes, 2.03)
    assert len(out_levels) == len(out_volumes) == 408
    assert out_levels[-2:] == [2.03, 2.03]
    assert errors == []
    assert warnings == []


def test_clean_table_sorts_and_flags_problems() -> None:
    levels, volumes, warnings, errors = clean_table([0.1, 0.0, 0.2], [1.0, 0.5, 2.0], 0.2)
    assert levels == [0.0, 0.1, 0.2] and volumes == [0.5, 1.0, 2.0]
    assert warnings == [] and errors == []

    _, _, _, errors = clean_table([0.0, 0.1, 0.1], [0.0, 1.0, 1.1], 0.1)
    assert any('two volumes' in e for e in errors)
    _, _, _, errors = clean_table([0.0, 0.1, 0.2], [0.0, 1.0, 0.9], 0.2)
    assert any('decreases' in e for e in errors)
    _, _, _, errors = clean_table([0.05, 0.1], [0.0, 1.0], 0.1)
    assert any('start at 0' in e for e in errors)
    _, _, warnings, errors = clean_table([0.0, 0.1], [0.0, 1.0], 0.2)
    assert errors == [] and any('Tank_Height' in w for w in warnings)


# ---- calibrate ----

@pytest.mark.parametrize('mode', [Mode.OFFSET, Mode.DIRECT])
def test_revised_matches_calibration_points(fo1_base: Base, fo1_points: list[Point], mode: Mode) -> None:
    result = run(fo1_base, fo1_points, mode)
    for p in fo1_points:
        assert revised_at(result, p.level) == pytest.approx(p.volume, abs=TOL)


def test_col2_is_always_base_volume(fo1_base: Base, fo1_points: list[Point]) -> None:
    levels, volumes, _ = fo1_base
    for mode in (Mode.NONE, Mode.DIRECT, Mode.OFFSET):
        result = run(fo1_base, fo1_points if mode is not Mode.NONE else [], mode)
        assert result.volumes == volumes


def test_modes_at_0_05(fo1_base: Base, fo1_points: list[Point]) -> None:
    assert revised_at(run(fo1_base, fo1_points, Mode.DIRECT), 0.05) == pytest.approx(0.135, abs=TOL)
    assert revised_at(run(fo1_base, fo1_points, Mode.OFFSET), 0.05) == pytest.approx(0.100, abs=TOL)
    assert revised_at(run(fo1_base, [], Mode.NONE), 0.05) == pytest.approx(0.110, abs=TOL)


@pytest.mark.parametrize(('level', 'expected'), [
    (0.10, 0.230), (0.11, 0.275), (0.12, 0.330), (0.15, 0.505), (0.18, 0.700), (0.20, 0.850),
])
def test_offset_worked_rows(fo1_base: Base, fo1_points: list[Point], level: float, expected: float) -> None:
    assert revised_at(run(fo1_base, fo1_points, Mode.OFFSET), level) == pytest.approx(expected, abs=TOL)


@pytest.mark.parametrize(('level', 'offset', 'direct'), [(0.05, 0.100, 0.135), (0.15, 0.505, 0.540), (1.95, 12.245, 12.250)])
def test_offset_vs_direct_table(fo1_base: Base, fo1_points: list[Point], level: float, offset: float,
                                direct: float) -> None:
    assert revised_at(run(fo1_base, fo1_points, Mode.OFFSET), level) == pytest.approx(offset, abs=TOL)
    assert revised_at(run(fo1_base, fo1_points, Mode.DIRECT), level) == pytest.approx(direct, abs=TOL)


@pytest.mark.parametrize('mode', list(Mode))
def test_output_levels_are_cleaned_base_levels(fo1_base: Base, fo1_points: list[Point], mode: Mode) -> None:
    levels, volumes, _ = fo1_base
    cleaned_levels, _, _, _ = clean_table(levels, volumes, 2.03)
    result = run(fo1_base, fo1_points, mode)
    assert result.levels == cleaned_levels
    assert len(result.volumes) == len(result.diffs) == 408


def test_none_mode_zero_diffs(fo1_base: Base) -> None:
    levels, volumes, _ = fo1_base
    result = run(fo1_base, [], Mode.NONE)
    assert result.volumes == volumes
    assert result.diffs == [0.0] * len(volumes)
    assert result.errors == []


def test_diff_vol_is_revised_minus_base(fo1_base: Base, fo1_points: list[Point]) -> None:
    result = run(fo1_base, fo1_points, Mode.DIRECT)
    for v, d, level in zip(result.volumes, result.diffs, result.levels, strict=True):
        assert d == pytest.approx(revised_at(result, level) - v, abs=TOL)
    assert revised_at(result, 0.05) == pytest.approx(0.135, abs=TOL)
    assert volume_at(result, 0.05) == pytest.approx(0.110, abs=TOL)
    assert result.diffs[result.levels.index(0.05)] == pytest.approx(0.025, abs=TOL)


def test_warnings(fo1_base: Base, fo1_points: list[Point]) -> None:
    result = run(fo1_base, fo1_points, Mode.DIRECT)
    assert '1.80–1.90 m: 2.00 m³/m vs base 6.80' in result.warnings
    assert '2.00–2.03 m: 16.67 m³/m vs base 6.67' in result.warnings
    assert result.errors == []


def test_fo1_offset_revised_dips(fo1_base: Base, fo1_points: list[Point]) -> None:
    """FO1 Offset revised volume dips by 2 L twice; COL2 stays the base table."""
    result = run(fo1_base, fo1_points, Mode.OFFSET)
    assert result.errors == []
    assert volume_at(result, 0) == pytest.approx(0.03, abs=TOL)
    assert volume_at(result, 0.005) == pytest.approx(0.03, abs=TOL)
    assert revised_at(result, 0) == pytest.approx(0.04, abs=TOL)
    assert revised_at(result, 0.005) == pytest.approx(0.038, abs=TOL)
    assert revised_at(result, 0.015) == pytest.approx(0.054, abs=TOL)
    assert revised_at(result, 0.02) == pytest.approx(0.052, abs=TOL)


# ---- validation errors ----

def test_calibration_missing_top(fo1_base: Base, fo1_points: list[Point]) -> None:
    result = run(fo1_base, fo1_points[:-1], Mode.OFFSET)
    assert any('last base level 2.03' in e for e in result.errors)
    assert result.volumes == []


def test_calibration_decreasing_volume() -> None:
    errors = validate_calibration([Point(0, 0), Point(0.1, 1.0), Point(0.2, 0.9)], [0.0, 0.2])
    assert any('volume decreases' in e for e in errors)


def test_calibration_same_level_two_volumes() -> None:
    errors = validate_calibration([Point(0, 0), Point(0.1, 1.0), Point(0.1, 1.2), Point(0.2, 2)], [0.0, 0.2])
    assert any('two volumes' in e for e in errors)


def test_calibration_too_few_points() -> None:
    assert validate_calibration([Point(0, 0)], [0.0, 0.2])


def test_calibration_within_1mm_of_ends_is_accepted() -> None:
    assert validate_calibration([Point(0.0005, 0), Point(0.1995, 2)], [0.0, 0.2]) == []


# ---- packet ----

@pytest.mark.parametrize('mode', [Mode.OFFSET, Mode.DIRECT])
def test_calibrated_packet(fo1_base: Base, fo1_points: list[Point], mode: Mode) -> None:
    result = run(fo1_base, fo1_points, mode)
    packet = packet_for(result)
    n = len(packet) // 4
    assert n == 8 + 3 * 408 == 1232
    assert n <= PLC_BUFFER_REALS
    vals = struct.unpack(f'<{n}f', packet)
    expected_header = struct.unpack('<8f', struct.pack('<8f', *(v for _, v in FO1_HEADER)))
    assert vals[:NUM_HEADER_REALS] == expected_header
    col2 = vals[NUM_HEADER_REALS + 408:NUM_HEADER_REALS + 2 * 408]
    col3 = vals[NUM_HEADER_REALS + 2 * 408:]
    for got, want in zip(col2, result.volumes, strict=True):
        assert got == pytest.approx(want, rel=1e-6, abs=1e-6)
    for got, want in zip(col3, result.diffs, strict=True):
        assert got == pytest.approx(want, rel=1e-6, abs=1e-6)


def test_calibrated_vs_raw_differ_only_in_col3(fo1_base: Base, fo1_points: list[Point]) -> None:
    raw = packet_for(run(fo1_base, fo1_points, Mode.NONE))
    cal = packet_for(run(fo1_base, fo1_points, Mode.DIRECT))
    assert len(raw) == len(cal)
    col3_start = (NUM_HEADER_REALS + 2 * 408) * 4
    assert raw[:col3_start] == cal[:col3_start]
    assert raw[col3_start:] != cal[col3_start:]


# ---- preview_reading ----

FO1_HEADER_MAP = dict(FO1_HEADER)


def _revised_volumes(result: CalibrationResult) -> list[float]:
    return [v + d for v, d in zip(result.volumes, result.diffs, strict=True)]


@pytest.mark.parametrize(('mode', 'expected'), [
    (Mode.NONE, 0.313911), (Mode.DIRECT, 0.329063), (Mode.OFFSET, 0.307878),
])
def test_preview_4_5_mA(fo1_base: Base, fo1_points: list[Point], mode: Mode, expected: float) -> None:
    result = run(fo1_base, fo1_points if mode is not Mode.NONE else [], mode)
    reading = preview_reading(4.5, FO1_HEADER_MAP, result.levels, _revised_volumes(result))
    assert reading['status'] == 'OK'
    assert reading['pressure_bar'] == pytest.approx(0.009375, abs=TOL)
    assert reading['level_m'] == pytest.approx(0.115978, abs=TOL)
    assert reading['volume_m3'] == pytest.approx(expected, abs=TOL)
    assert reading['litres'] == pytest.approx(expected * 1000, abs=0.1)
    assert reading['tonnes'] == pytest.approx(expected * 0.824, abs=TOL)


@pytest.mark.parametrize('mA', [3.5, 21.0])
def test_preview_sensor_fault(fo1_base: Base, fo1_points: list[Point], mA: float) -> None:
    result = run(fo1_base, [], Mode.NONE)
    assert preview_reading(mA, FO1_HEADER_MAP, result.levels, _revised_volumes(result))['status'] == 'SENSOR_FAULT'


def test_preview_over_range(fo1_base: Base, fo1_points: list[Point]) -> None:
    result = run(fo1_base, [], Mode.NONE)
    reading = preview_reading(15.0, FO1_HEADER_MAP, result.levels, _revised_volumes(result))
    assert reading['status'] == 'OVER_RANGE'
    assert reading['level_m'] == pytest.approx(2.03, abs=TOL)
    assert reading['level_pct'] == pytest.approx(100, abs=TOL)
    assert reading['volume_pct'] == pytest.approx(100, abs=TOL)


def test_preview_below_4mA_clamps_to_empty(fo1_base: Base, fo1_points: list[Point]) -> None:
    result = run(fo1_base, [], Mode.NONE)
    reading = preview_reading(3.9, FO1_HEADER_MAP, result.levels, _revised_volumes(result))
    assert reading['status'] == 'OK'
    assert reading['level_m'] == 0


@pytest.mark.parametrize('mode', [
    Mode.NONE,
    Mode.DIRECT,
    pytest.param(Mode.OFFSET, marks=pytest.mark.xfail(
        strict=True, reason='FO1 Offset revised table dips at 0–0.02 m')),
])
def test_preview_volume_non_decreasing(fo1_base: Base, fo1_points: list[Point], mode: Mode) -> None:
    result = run(fo1_base, fo1_points if mode is not Mode.NONE else [], mode)
    vols = _revised_volumes(result)

    @given(st.floats(4, 20), st.floats(4, 20))
    @example(4.0, 4.0216)  # 0 m → 0.005 m, where FO1 Offset dips
    def check(a: float, b: float) -> None:
        lo, hi = sorted((a, b))
        v_lo = preview_reading(lo, FO1_HEADER_MAP, result.levels, vols)['volume_m3']
        v_hi = preview_reading(hi, FO1_HEADER_MAP, result.levels, vols)['volume_m3']
        assert v_lo is not None and v_hi is not None
        assert v_lo <= v_hi

    check()


# ---- tank file ----

def test_workbook_calibration_comes_from_second_sheet(fo1_points: list[Point]) -> None:
    loaded = load_tank_file(str(Path(__file__).parents[1] / '1_FUEL OIL TANK 1.xlsx'))
    assert list(loaded.table.columns) == ['Level_m', 'Volume_m3', 'Weight']
    assert len(loaded.table) == 408
    assert loaded.calibration_sheet == 'Sheet2'
    assert loaded.points == fo1_points
    assert loaded.points[0] == Point(0.0, 0.04)
    assert loaded.points[-1] == Point(2.03, 13.0)


def test_csv_sounding_table_has_no_calibration_sheet() -> None:
    loaded = load_tank_file(str(FIXTURES / 'fo1_base.csv'))
    assert len(loaded.table) == 408
    assert loaded.points == []
    assert loaded.calibration_sheet is None


def test_fo1_diff_vol_matches_fuel_oil_tank_sheet(fo1_base: Base, fo1_points: list[Point]) -> None:
    """App Direct Diff.Vol matches Fuel_Oil_Tank_1 Linear Corrected − Volume_m3."""
    result = run(fo1_base, fo1_points, Mode.DIRECT)
    assert revised_at(result, 0.0) == pytest.approx(0.04, abs=TOL)
    assert revised_at(result, 0.005) == pytest.approx(0.0495, abs=TOL)
    assert result.diffs[result.levels.index(0.0)] == pytest.approx(0.01, abs=TOL)
    assert result.diffs[result.levels.index(0.1)] == pytest.approx(-0.03, abs=TOL)
    assert result.diffs[result.levels.index(0.05)] == pytest.approx(0.025, abs=TOL)
