"""Tank presets. Python constants so the PyInstaller build needs no --add-data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TankPreset:
    number: int
    name: str
    rtu: float
    sensor_min_bar: float
    sensor_max_bar: float
    density: float          # kg/m³
    dead_volume: float      # m³
    tank_height: float      # m

    @property
    def label(self) -> str:
        return f'{self.number}. {self.name}'


# RTU 4 = tanks 1–8, RTU 5 = tanks 9–12
TANKS: tuple[TankPreset, ...] = (
    TankPreset(1, 'FUEL OIL TANK 01', 4.0, 0.0, 0.3, 824.0, 0.03, 2.03),
    TankPreset(2, 'FUEL OIL TANK 02', 4.0, 0.0, 0.3, 824.0, 0.00, 1.98),
    TankPreset(3, 'FUEL OIL TANK 03', 4.0, 0.0, 0.2, 824.0, 0.03, 0.708),
    TankPreset(4, 'FO RU TANK 01', 4.0, 0.0, 0.2, 824.0, 0.03, 2.075),
    TankPreset(5, 'FO RU TANK 02', 4.0, 0.0, 0.2, 824.0, 0.03, 2.076),
    TankPreset(6, 'FO RU TANK 03', 4.0, 0.0, 0.2, 824.0, 0.04, 2.075),
    TankPreset(7, 'FO RU TANK 04', 4.0, 0.0, 0.2, 824.0, 0.04, 2.075),
    TankPreset(8, 'LO TANK', 4.0, 0.0, 0.2, 870.0, 0.14, 0.8),
    TankPreset(9, 'FRESH WATER TANK', 5.0, 0.0, 0.3, 1000.0, 0.07, 2.152),
    TankPreset(10, 'GREY WATER TANK', 5.0, 0.0, 0.3, 1025.0, 0.01, 2.196),
    TankPreset(11, 'BILGE OIL TANK', 5.0, 0.0, 0.2, 824.0, 0.01, 0.896),
    TankPreset(12, 'DIRTY OIL TANK', 5.0, 0.0, 0.2, 824.0, 0.01, 0.886),
)


def preset_header_fields(tank: TankPreset) -> dict[str, float]:
    """Header field values (keyed like app.FRAME_HEADER_FIELDS) that a preset fills in."""
    return {
        'sensor_min_bar': tank.sensor_min_bar,
        'sensor_max_bar': tank.sensor_max_bar,
        'density': tank.density,
        'dead_volume': tank.dead_volume,
        'tank_height': tank.tank_height,
    }
