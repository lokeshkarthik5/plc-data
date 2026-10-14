# Sounding-table calibration — implementation spec

Audience: Claude Code working in this repo. Read `app.py` fully before changing anything. Work in the commit order in §9 and run tests after each step.

> **Change 1 (supersedes the calibration CSV):** base table and calibration points both come from **one .xlsx workbook**, from different sheets. See §0. §1–§9 are already implemented on this branch; §0 lists only what changes.

## 0. Change 1 — single workbook input

### 0.1 Source layout (real file: `tests/fixtures/Tank_Interpolation_Calibration.xlsx`)

| What | Sheet | Header row | Columns | Rows |
|---|---|---|---|---|
| Base table | `Sounding Table` | 1 | A `Level_m`, B `Volume_m3`, C `Weight` | 2–409 (408 rows, final row duplicated) |
| Calibration points | `Sounding Table_Calibrated` | 3 | H `Level_m`, I `Corrected_Volume_m3` | 4–25 (22 points) |

Every other column in the calibration sheet (A–C copy of base, D/F/J/M derived, L, AF–AI scratch) is **ignored**. The calibration sheet has four columns headed `Level_m` (A, H, L, AF) — header matching must anchor on `Corrected_Volume_m3`, not on `Level_m`.

### 0.2 New module `tankgauge/workbook.py`

- `list_sheets(path) -> list[str]`
- `read_base_sheet(path, sheet) -> pandas.DataFrame` — header row 1, all columns; feeds the existing 3-column mapping unchanged.
- `find_calibration_columns(path, sheet) -> CalibrationSource | None` — scan rows 1–10 for a cell whose text, case-insensitive, contains `corrected` and `volume`. Volume column = that column; level column = nearest column **to its left** whose header contains `level`. Header row = that row. Return `None` if not found.
- `read_calibration_points(path, sheet, level_col, volume_col, header_row) -> (list[Point], str)` — read numeric rows below the header until the first blank level cell; return points plus a source label like `"Sounding Table_Calibrated!H4:I25"`.
- Open with `openpyxl.load_workbook(path, read_only=True, data_only=True)`. If a selected cell is a formula with no cached value (`None` where a number is expected inside the range), raise `ValueError("… open and save the workbook in Excel, then reload")`.
- Strip whitespace/non-breaking spaces from headers before matching.

### 0.3 UI changes

1. File picker (`Browse…`) accepts `*.xlsx` and `*.csv`. CSV keeps today's behaviour (base table only, calibration disabled, mode forced to None).
2. For `.xlsx`, add two comboboxes under the file label:
   - **Base sheet** — default `Sounding Table` if present, else first sheet. Changing it reloads the base table into the existing column mapping.
   - **Calibration sheet** — `(none)` + all sheets; default the first sheet where `find_calibration_columns` succeeds (→ `Sounding Table_Calibrated`).
3. If the chosen calibration sheet has no detectable columns, show two column comboboxes (level, corrected volume) populated from that sheet's header candidates in rows 1–10; user picks, then points load.
4. **Remove** the "Load calibration CSV…" button, `pick_calibration_file`, and `load_points_csv` (and its tests).
5. Calibration sheet `(none)` → mode forced to None and the mode combobox disabled.
6. Log on load: `Base: 'Sounding Table' 408 rows · Calibration: Sounding Table_Calibrated!H4:I25, 22 points`. Use the same label in the send-session log instead of the calibration filename.
7. Calculation (§2), send path, packet and validation are **unchanged**.

### 0.4 Dependencies / build

- `requirements.txt`: add `openpyxl`.
- `build.yml`: install it; add `--hidden-import openpyxl` to the `pyinstaller` command.

### 0.5 Tests (`tests/test_workbook.py`, using the fixture xlsx)

- `list_sheets` returns the 4 sheet names.
- `read_base_sheet(..., 'Sounding Table')` → 408 rows; first row (0, 0.03, 0.02); last row (2.03, 13.06, 11.1).
- `find_calibration_columns(..., 'Sounding Table_Calibrated')` → volume col I, level col H, header row 3 (**not** A, L or AF).
- `read_calibration_points` → 22 points equal to `tests/fixtures/fo1_calibration.csv`; label `Sounding Table_Calibrated!H4:I25`.
- `find_calibration_columns(..., 'Sounding Table')` → `None`.
- End-to-end: workbook → clean → Offset calibrate → packet = 1229 REALs; COL2 at 0.15 m = 0.505.
- Existing calibration/packet tests stay green.

### 0.6 Commit order

1. `workbook.py` + tests + fixture. 2. UI: xlsx picker + sheet comboboxes; remove calibration CSV path. 3. Build deps. Run `pytest` after each.

## 1. Context

`app.py` is a Tkinter tool ("CSV to PLC"). It loads a CSV sounding table, maps 3 columns and sends **one UDP multicast packet every 100 ms** to a CODESYS PLC:

```
float32 LE, column-major
[0] RTU_Number  [1] Tank_Number  [2] Column_Size  [3] Sensor_Min_bar
[4] Sensor_Max_bar  [5] Density  [6] Dead_Volume  [7] Tank_Height
[8 .. 8+N-1]      COL1 = level (m)
[8+N .. 8+2N-1]   COL2 = volume (m³)
[8+2N .. 8+3N-1]  COL3 = weight (t)
```

The **PLC** converts mA → level and interpolates volume from this table. This tool does not compute volumes at runtime.

**Goal:** apply on-site calibration points to the base sounding table and send the **calibrated** table through the existing path.

**Hard constraints**
- Packet format, header layout, multicast group/port, single-packet-per-round, `ROUND_DELAY_SEC` loop and Stop behaviour are unchanged.
- The only difference on the wire: COL1/COL2/COL3 hold the cleaned + calibrated level/volume/weight arrays.
- PLC code is untouched.
- `pyinstaller --onefile --windowed --name CSV_to_PLC app.py` must still build.

## 2. The calculation

Inputs:
- **Base table** — shipyard sounding table, (level, volume) every 5 mm. FO Tank 1: 408 rows, 0 → 2.03 m.
- **Calibration points** — (level, corrected volume) measured on site, every 0.1 m. FO Tank 1: 22 points. Read from a separate sheet of the same workbook (§0).

Output levels are **always the base table's levels** (same 5 mm grid, same row count after de-duplication). Only volumes and weights change.

### 2.1 Offset mode (default)

**Step 1 — correction at each calibration point (once):**

```
corr_i = corrected_volume_i − interp(base, level_i)
```

| Level | Corrected | Base | corr |
|---|---|---|---|
| 0.0 | 0.04 | 0.03 | +0.01 |
| 0.1 | 0.23 | 0.26 | −0.03 |
| 0.2 | 0.85 | 0.73 | +0.12 |
| 0.3 | 1.15 | 1.39 | −0.24 |
| … | | | |
| 2.03 | 13.00 | 13.06 | −0.06 |

**Step 2 — for every base row (L, B):**

```
find L1 ≤ L < L2 among calibration levels
corr(L) = corr1 + (L − L1)/(L2 − L1) × (corr2 − corr1)
V       = B + corr(L)
W       = V × density / 1000
```

Worked rows, segment 0.1–0.2 m (corr −0.03 → +0.12):

| Level | Base B | corr(L) | Calibrated V |
|---|---|---|---|
| 0.10 | 0.26 | −0.030 | **0.230** (= calibration point) |
| 0.11 | 0.29 | −0.015 | **0.275** |
| 0.12 | 0.33 | 0.000 | **0.330** |
| 0.15 | 0.46 | +0.045 | **0.505** |
| 0.18 | 0.61 | +0.090 | **0.700** |
| 0.20 | 0.73 | +0.120 | **0.850** (= calibration point) |

Why Offset: keeps the base table's shape between calibration points (matters in the curved bottom 0–0.2 m).

### 2.2 Direct mode (matches the original spreadsheet)

Ignore B; interpolate corrected volumes directly:

```
V = V1 + (L − L1)/(L2 − L1) × (V2 − V1)
```

| Level | Offset | Direct |
|---|---|---|
| 0.05 | 0.100 | 0.135 |
| 0.15 | 0.505 | 0.540 |
| 1.95 | 12.245 | 12.250 |

Both modes equal the corrected volume exactly at every calibration level.

### 2.3 None mode

Base volumes unchanged (still de-duplicated).

### 2.4 What the PLC then does (for the preview panel only)

```
P_bar = min + (mA − 4)/16 × (max − min)
L     = P_bar × 100000 / (density × 9.81)
V     = linear interp between the two neighbouring 5 mm rows of the received table
```

FO Tank 1 at 4.5 mA (0–0.3 bar, 824 kg/m³): P = 0.009375 bar, L = 0.115978 m → V: None 0.313911, Direct 0.329063, Offset 0.307878.

## 3. Repo restructure

```
app.py                      UI only
tankgauge/__init__.py
tankgauge/packet.py         build_combined_packet, describe_combined_packet, packet constants (moved unchanged)
tankgauge/tanks.py          tank presets (Python constants, no data files → no --add-data)
tankgauge/calibration.py    pure logic, no Tkinter; pandas only at edges
tests/test_packet.py
tests/test_calibration.py
tests/fixtures/fo1_base.csv         FO Tank 1 base table (408 rows incl. duplicate final row)
tests/fixtures/fo1_calibration.csv  FO Tank 1 calibration points (22 rows)
requirements.txt            pandas
requirements-dev.txt        pytest, hypothesis, ruff, mypy
```

## 4. tanks.py — presets

`TANK_OPTIONS` in app.py is derived from this. Selecting a tank fills the header fields (still editable).

| # | Name | RTU | Min bar | Max bar | Density | Dead vol | Height |
|---|---|---|---|---|---|---|---|
| 1 | FUEL OIL TANK 01 | 4 | 0 | 0.3 | 824 | 0.03 | 2.03 |
| 2 | FUEL OIL TANK 02 | 4 | 0 | 0.3 | 824 | 0.00 | 1.98 |
| 3 | FUEL OIL TANK 03 | 4 | 0 | 0.2 | 824 | 0.03 | 0.708 |
| 4 | FO RU TANK 01 | 4 | 0 | 0.2 | 824 | 0.03 | 2.075 |
| 5 | FO RU TANK 02 | 4 | 0 | 0.2 | 824 | 0.03 | 2.076 |
| 6 | FO RU TANK 03 | 4 | 0 | 0.2 | 824 | 0.04 | 2.075 |
| 7 | FO RU TANK 04 | 4 | 0 | 0.2 | 824 | 0.04 | 2.075 |
| 8 | LO TANK | 4 | 0 | 0.2 | 870 | 0.14 | 0.8 |
| 9 | FRESH WATER TANK | 5 | 0 | 0.3 | 1000 | 0.07 | 2.152 |
| 10 | GREY WATER TANK | 5 | 0 | 0.3 | 1025 | 0.01 | 2.196 |
| 11 | BILGE OIL TANK | 5 | 0 | 0.2 | 824 | 0.01 | 0.896 |
| 12 | DIRTY OIL TANK | 5 | 0 | 0.2 | 824 | 0.01 | 0.886 |

## 5. calibration.py

Types: frozen `@dataclass(slots=True)` — `Point(level, volume)`, `CalibrationResult(levels, volumes, weights, warnings: list[str], errors: list[str])`. `Mode(StrEnum)`: `NONE`, `OFFSET`, `DIRECT`. Python 3.11+, float64 internally, no rounding.

**`interp(levels, values, x)`** — `bisect_right` to find `levels[i] ≤ x < levels[i+1]`; linear interpolation; `x == levels[-1]` returns last value; out of range → `ValueError`; never index past the end.

**`clean_table(levels, volumes, tank_height)`** → `(levels, volumes, warnings, errors)`
- Sort by level; drop exact duplicate rows (warn with count). FO1 has a duplicated final 2.03 row — a zero-width segment can divide by zero in the PLC.
- Same level, different volume → error.
- Volume decreasing with level → error.
- First level ≠ 0 → error.
- Last level ≠ tank_height (±1 mm) → warning.

**`validate_calibration(points, base_levels)`**
- ≥ 2 points, strictly increasing levels, non-decreasing volumes.
- Must include first and last base level (±1 mm) → otherwise error. Never extrapolate.

**`calibrate(base_levels, base_volumes, base_weights, points, mode, density)`** → `CalibrationResult`
- Modes per §2. Output levels == cleaned base levels.
- Weights: `V × density / 1000`. If the base table's median weight/volume ratio (rows with V > 0) differs from `density/1000` by > 1% → warning naming both (FO1: 0.85 vs 0.824).
- Shape check (warnings only): per calibration segment compare result ΔV/ΔL with base ΔV/ΔL over the same span; warn if > 25% off, e.g. `"1.90–2.00 m: 2.00 m³/m vs base 6.70"`. FO1 data is expected to trigger several.
- Output volumes must be non-decreasing → error otherwise.

**`preview_reading(mA, header, levels, volumes)`** → dict (UI only, mirrors §2.4)
- `mA < 3.8` or `> 20.5` → `SENSOR_FAULT` (NAMUR NE43).
- Clamp L to `[0, tank_height]`; clamped at top → `OVER_RANGE`; else `OK`.
- Returns status, pressure_bar, level_m, volume_m3, litres, tonnes, level_pct, volume_pct.
- Does **not** add dead volume.

**`load_points_csv(path)`** — 2-column CSV (level, volume); skip non-numeric rows like app.py does.

## 6. UI changes (app.py)

1. **Tank presets** — selecting a tank fills Sensor_Min_bar, Sensor_Max_bar, Density, Dead_Volume, Tank_Height.
2. **Calibration LabelFrame** (below Column mapping):
   - "Load calibration CSV…" button + file label.
   - Mode combobox: None / Offset (recommended) / Direct.
   - "Show comparison" → log per calibration level: base V, calibrated V, Δ; then per-segment area vs base; then all warnings.
   - Status line: `Calibrated 407 rows · 6 warnings · 0 errors` (red errors / orange warnings / green clean).
3. **Send path** — packet built from calibrated table when mode ≠ None, else from cleaned base table (de-dup always applies). `Column_Size` = calibrated row count. Errors block send (`showerror`). Warnings need `askyesno` listing up to 10. Log mode + calibration filename at session start. Same `build_combined_packet`, group/port, single packet per round, `ROUND_DELAY_SEC`, Stop behaviour.
4. **Check reading** — mA entry + "Check" button → logs `preview_reading` for the table selected for sending. Show Dead_Volume separately with note: *"PLC may add Dead_Volume; base table already reads 0.03 m³ at 0 m — check for double counting."*
5. Recalibrate on change of CSV, column mapping, calibration file, mode, or Density. Synchronous is fine.
6. Networking code untouched except the data source.

## 7. Tests (abs tolerance 1e-4)

Fixtures: `tests/fixtures/fo1_base.csv`, `tests/fixtures/fo1_calibration.csv`.

- `clean_table`: 408 → 407 rows, 1 duplicate dropped, last level 2.03.
- Offset and Direct equal the calibration volume at every calibration level.
- At 0.05 m: Direct 0.135, Offset 0.100, None 0.110.
- Worked rows in §2.1 (0.11 → 0.275, 0.15 → 0.505, 0.18 → 0.700) in Offset.
- Output levels identical to cleaned base levels in every mode.
- Calibrated FO1 packet = 8 + 3×407 = 1229 REALs (≤ `PLC_BUFFER_REALS` 1361); header unchanged; decoded COL2 == calibrated volumes within float32 precision.
- Calibrated vs raw send of the same base table differ only in COL2/COL3 sections.
- `preview_reading` 4.5 mA FO1: level 0.115978; None 0.313911, Direct 0.329063, Offset 0.307878.
- 3.5 / 21 mA → `SENSOR_FAULT`; 15 mA → `OVER_RANGE`, level 2.03.
- Warnings include weight/density mismatch and ≥ 1 shape-check warning.
- Errors: calibration missing 2.03; decreasing calibration volume; same level two volumes.
- Hypothesis: random mA in [4, 20] → volume non-decreasing in all modes.
- `SAMPLE_CSV_ROWS` packet bytes identical before/after refactor — capture the hex from current code first, hard-code it.

## 8. Tooling

- `.github/workflows/build.yml`: install `requirements.txt` + `requirements-dev.txt`, run `pytest` before `pyinstaller`; fail build on test failure.
- Type hints on `calibration.py` and `packet.py` (`mypy --strict` clean); `ruff` clean.

## 9. Commit order

1. Capture current `SAMPLE_CSV_ROWS` packet hex → extract `packet.py` → byte-pinning test.
2. `tanks.py` + preset autofill.
3. `calibration.py` + tests.
4. Calibration frame + send path.
5. Check-reading panel.
6. CI.

## 10. Open issues (not for code to decide — flag, don't fix)

- **Calibration data quality.** FO1 corrected volumes imply cross-section 1.9–16.7 m³/m per segment vs a steady 6.7 in the base table; corrections alternate sign and values look rounded. Treat calibrated output as provisional until readings are re-verified.
- **Weight vs density.** Base table weight = 0.85 × volume; header density = 824. Spec recalculates weight from header density. Confirm which is correct.
- **Dead volume.** Base table reads 0.03 m³ at 0 m and Dead_Volume = 0.03 is sent; if the PLC adds it, volume is double-counted. Check the PLC code.
- **Sensor range.** FO Tank 1: 0.3 bar at 824 kg/m³ = 3.71 m span vs 2.03 m tank → saturates at 12.75 mA. Hardware/config issue, out of scope.
- **mA fault handling** happens in the PLC; `preview_reading` only mirrors the intended behaviour.
