"""Sounding-table calibration (docs/calibration-spec.md §2, §5).

Pure logic, no Tkinter. pandas is used only in load_points_csv. All maths is
float64 with no rounding; float32 conversion happens when the packet is built.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import TypedDict

import pandas as pd

LEVEL_TOL_M = 0.001             # ±1 mm when matching levels against each other
WEIGHT_RATIO_TOL = 0.01         # base weight/volume vs density/1000
SHAPE_TOL = 0.25                # calibrated vs base cross-section per segment
GRAVITY = 9.81
PA_PER_BAR = 100000.0
NAMUR_MIN_MA = 3.8              # NAMUR NE43 fault limits
NAMUR_MAX_MA = 20.5


class Mode(StrEnum):
    NONE = 'none'
    OFFSET = 'offset'
    DIRECT = 'direct'


@dataclass(frozen=True, slots=True)
class Point:
    level: float
    volume: float


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    levels: list[float]
    volumes: list[float]
    weights: list[float]
    warnings: list[str]
    errors: list[str]


class Reading(TypedDict):
    status: str                 # OK | OVER_RANGE | SENSOR_FAULT
    pressure_bar: float | None
    level_m: float | None
    volume_m3: float | None
    litres: float | None
    tonnes: float | None
    level_pct: float | None
    volume_pct: float | None


def interp(levels: Sequence[float], values: Sequence[float], x: float) -> float:
    """Linear interpolation on levels[i] ≤ x < levels[i+1]; levels must be non-decreasing."""
    if len(levels) != len(values) or not levels:
        raise ValueError('levels and values must be non-empty and the same length')
    if x < levels[0] or x > levels[-1]:
        raise ValueError(f'{x} is outside the table range {levels[0]}..{levels[-1]}')
    if x == levels[-1]:
        return float(values[-1])
    # bisect_right skips past any repeated level, so levels[i + 1] > x ≥ levels[i].
    i = bisect_right(levels, x) - 1
    l1, l2 = levels[i], levels[i + 1]
    v1, v2 = values[i], values[i + 1]
    return float(v1 + (x - l1) / (l2 - l1) * (v2 - v1))


def _interp_clamped(levels: Sequence[float], values: Sequence[float], x: float) -> float:
    return interp(levels, values, min(max(x, levels[0]), levels[-1]))


def _clean_indices(
    levels: Sequence[float], volumes: Sequence[float], tank_height: float | None,
) -> tuple[list[int], list[str], list[str]]:
    """Row indices kept after sorting by level and dropping exact duplicates."""
    if len(levels) != len(volumes):
        raise ValueError('levels and volumes must be the same length')
    warnings: list[str] = []
    errors: list[str] = []
    if not levels:
        return [], warnings, ['Base table is empty.']

    order = sorted(range(len(levels)), key=lambda i: levels[i])
    kept: list[int] = []
    dropped = 0
    for i in order:
        if kept:
            prev = kept[-1]
            if levels[i] == levels[prev]:
                if volumes[i] == volumes[prev]:
                    dropped += 1
                    continue
                errors.append(
                    f'Level {levels[i]:g} m has two volumes: {volumes[prev]:g} and {volumes[i]:g} m³.'
                )
            elif volumes[i] < volumes[prev]:
                errors.append(
                    f'Volume decreases from {volumes[prev]:g} m³ at {levels[prev]:g} m '
                    f'to {volumes[i]:g} m³ at {levels[i]:g} m.'
                )
        kept.append(i)

    if dropped:
        warnings.append(f'Dropped {dropped} duplicate row(s) from the base table.')
    if levels[kept[0]] != 0:
        errors.append(f'Base table must start at 0 m (first level is {levels[kept[0]]:g} m).')
    if tank_height is not None and abs(levels[kept[-1]] - tank_height) > LEVEL_TOL_M:
        warnings.append(
            f'Base table ends at {levels[kept[-1]]:g} m but Tank_Height is {tank_height:g} m.'
        )
    return kept, warnings, errors


def clean_table(
    levels: Sequence[float], volumes: Sequence[float], tank_height: float | None,
) -> tuple[list[float], list[float], list[str], list[str]]:
    """Sort by level, drop exact duplicate rows and check the table is usable.

    tank_height=None skips the top-of-table check.
    """
    kept, warnings, errors = _clean_indices(levels, volumes, tank_height)
    return [float(levels[i]) for i in kept], [float(volumes[i]) for i in kept], warnings, errors


def validate_calibration(points: Sequence[Point], base_levels: Sequence[float]) -> list[str]:
    """Errors that make the calibration points unusable against this base table."""
    errors: list[str] = []
    if len(points) < 2:
        return [f'Need at least 2 calibration points, got {len(points)}.']
    for p1, p2 in zip(points, points[1:], strict=False):
        if p2.level == p1.level:
            errors.append(
                f'Calibration level {p1.level:g} m has two volumes: {p1.volume:g} and {p2.volume:g} m³.'
            )
        elif p2.level < p1.level:
            errors.append(f'Calibration levels must increase: {p1.level:g} m is followed by {p2.level:g} m.')
        if p2.volume < p1.volume:
            errors.append(
                f'Calibration volume decreases from {p1.volume:g} m³ at {p1.level:g} m '
                f'to {p2.volume:g} m³ at {p2.level:g} m.'
            )
    if base_levels:
        first, last = base_levels[0], base_levels[-1]
        if abs(points[0].level - first) > LEVEL_TOL_M:
            errors.append(
                f'Calibration must include the first base level {first:g} m '
                f'(starts at {points[0].level:g} m).'
            )
        if abs(points[-1].level - last) > LEVEL_TOL_M:
            errors.append(
                f'Calibration must include the last base level {last:g} m '
                f'(ends at {points[-1].level:g} m); calibration is never extrapolated.'
            )
    return errors


def _weight_ratio_warning(
    volumes: Sequence[float], weights: Sequence[float], density: float, action: str,
) -> str | None:
    ratios = [w / v for v, w in zip(volumes, weights, strict=True) if v > 0]
    if not ratios:
        return None
    ratio = median(ratios)
    expected = density / 1000
    if expected > 0 and abs(ratio - expected) / expected <= WEIGHT_RATIO_TOL:
        return None
    return (
        f'Base table weight/volume ratio is {ratio:.3f} but Density/1000 is {expected:.3f}; {action}.'
    )


def _shape_warnings(
    points: Sequence[Point],
    base_levels: Sequence[float], base_volumes: Sequence[float],
    levels: Sequence[float], volumes: Sequence[float],
) -> list[str]:
    """Compare calibrated vs base cross-section (ΔV/ΔL) over each calibration segment."""
    warnings: list[str] = []
    for p1, p2 in zip(points, points[1:], strict=False):
        span = p2.level - p1.level
        result = (_interp_clamped(levels, volumes, p2.level) - _interp_clamped(levels, volumes, p1.level)) / span
        base = (
            _interp_clamped(base_levels, base_volumes, p2.level)
            - _interp_clamped(base_levels, base_volumes, p1.level)
        ) / span
        if abs(result - base) > SHAPE_TOL * abs(base):
            warnings.append(f'{p1.level:.2f}–{p2.level:.2f} m: {result:.2f} m³/m vs base {base:.2f}')
    return warnings


def calibrate(
    base_levels: Sequence[float],
    base_volumes: Sequence[float],
    base_weights: Sequence[float],
    points: Sequence[Point],
    mode: Mode,
    density: float,
    tank_height: float | None = None,
) -> CalibrationResult:
    """Apply calibration points to the base table (spec §2).

    Output levels are always the cleaned base levels. In NONE mode the cleaned
    base volumes and weights are returned unchanged; otherwise weights are
    V × density / 1000. tank_height is only used for the clean_table warning.
    """
    if len(base_weights) != len(base_volumes):
        raise ValueError('base_volumes and base_weights must be the same length')
    kept, warnings, errors = _clean_indices(base_levels, base_volumes, tank_height)
    levels = [float(base_levels[i]) for i in kept]
    base_v = [float(base_volumes[i]) for i in kept]
    base_w = [float(base_weights[i]) for i in kept]

    if mode is Mode.NONE:
        ratio_warning = _weight_ratio_warning(base_v, base_w, density, 'weights are sent as in the base table')
        if ratio_warning:
            warnings.append(ratio_warning)
        return CalibrationResult(levels, base_v, base_w, warnings, errors)

    errors.extend(validate_calibration(points, levels))
    if errors:
        return CalibrationResult([], [], [], warnings, errors)

    cal_levels = [p.level for p in points]
    if mode is Mode.OFFSET:
        corrections = [p.volume - _interp_clamped(levels, base_v, p.level) for p in points]
        volumes = [b + _interp_clamped(cal_levels, corrections, lv) for lv, b in zip(levels, base_v, strict=True)]
    else:
        cal_volumes = [p.volume for p in points]
        volumes = [_interp_clamped(cal_levels, cal_volumes, lv) for lv in levels]
    weights = [v * density / 1000 for v in volumes]

    ratio_warning = _weight_ratio_warning(base_v, base_w, density, 'weights are recalculated from Density')
    if ratio_warning:
        warnings.append(ratio_warning)
    warnings.extend(_shape_warnings(points, levels, base_v, levels, volumes))
    for i in range(1, len(volumes)):
        if volumes[i] < volumes[i - 1]:
            errors.append(
                f'Calibrated volume decreases from {volumes[i - 1]:.6g} m³ at {levels[i - 1]:g} m '
                f'to {volumes[i]:.6g} m³ at {levels[i]:g} m.'
            )
    return CalibrationResult(levels, volumes, weights, warnings, errors)


def preview_reading(
    mA: float, header: Mapping[str, float], levels: Sequence[float], volumes: Sequence[float],
) -> Reading:
    """What the PLC should show for a loop current (spec §2.4). Does not add Dead_Volume.

    header is keyed by packet header labels (Sensor_Min_bar, Sensor_Max_bar, Density, Tank_Height).
    """
    if mA < NAMUR_MIN_MA or mA > NAMUR_MAX_MA:
        return Reading(
            status='SENSOR_FAULT', pressure_bar=None, level_m=None, volume_m3=None,
            litres=None, tonnes=None, level_pct=None, volume_pct=None,
        )
    p_min, p_max = header['Sensor_Min_bar'], header['Sensor_Max_bar']
    density, tank_height = header['Density'], header['Tank_Height']

    pressure = p_min + (mA - 4) / 16 * (p_max - p_min)
    raw_level = pressure * PA_PER_BAR / (density * GRAVITY)
    level = min(max(raw_level, 0.0), tank_height)
    status = 'OVER_RANGE' if raw_level > tank_height else 'OK'
    volume = _interp_clamped(levels, volumes, level)
    full = volumes[-1]
    return Reading(
        status=status,
        pressure_bar=pressure,
        level_m=level,
        volume_m3=volume,
        litres=volume * 1000,
        tonnes=volume * density / 1000,
        level_pct=level / tank_height * 100 if tank_height else None,
        volume_pct=volume / full * 100 if full else None,
    )


def load_points_csv(path: str) -> list[Point]:
    """Calibration points from the first two CSV columns (level, volume); non-numeric rows are skipped."""
    df = pd.read_csv(path)
    if df.shape[1] < 2:
        raise ValueError(f'Calibration CSV needs 2 columns (level, volume), found {df.shape[1]}')
    numeric = df.iloc[:, :2].apply(pd.to_numeric, errors='coerce').dropna()
    return [Point(float(lv), float(v)) for lv, v in numeric.itertuples(index=False)]
