import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from tankgauge.calibration import Mode, calibrate, load_tank_file
from tankgauge.packet import (
    COLUMN_LABELS,
    NUM_COLUMNS,
    NUM_HEADER_REALS,
    PLC_BUFFER_REALS,
    REAL_SIZE,
    build_combined_packet,
    packet_format_summary,
)
from tankgauge.tanks import TANKS, preset_header_fields

# ---- UDP multicast defaults ----
MULTICAST_GROUP = '239.255.43.21'
RTU_LISTEN_PORT = 4001     # PLC listen port (SCADA -> RTU)
RTU_SEND_PORT = 9001      # PLC send port (RTU -> SCADA)
ROUND_DELAY_SEC = 0.1       # Pause between complete send rounds

# Tank list: RTU 4 = tanks 1–8, RTU 5 = tanks 9–12 (see tankgauge.tanks)
TANK_OPTIONS = tuple(
    {'tank': t.number, 'rtu': t.rtu, 'name': t.label, 'preset': t} for t in TANKS
)

# Remaining header REALs (after RTU_Number and Tank_Number)
FRAME_HEADER_FIELDS = (
    ('column_size', 'Column_Size', ''),
    ('sensor_min_bar', 'Sensor_Min_bar', '0'),
    ('sensor_max_bar', 'Sensor_Max_bar', '0'),
    ('density', 'Density', '0'),
    ('dead_volume', 'Dead_Volume', '0'),
    ('tank_height', 'Tank_Height', '0'),
)

SAMPLE_CSV_ROWS = (
    {'Channel': 101, 'PAO': 1.11, 'DAO': 9.91},
    {'Channel': 102, 'PAO': 2.22, 'DAO': 8.82},
    {'Channel': 103, 'PAO': 3.33, 'DAO': 7.73},
)

MAX_LISTED_ISSUES = 10


class CSVtoPLCApp:
    def __init__(self, root):
        self.root = root
        self.root.title('CSV to PLC')
        self.root.geometry('700x900')

        # Retina/HiDPI scaling fix for Mac
        try:
            self.root.tk.call('tk', 'scaling', 2.0)
        except tk.TclError:
            pass

        self.raw_df = None
        self.df = None
        self.filepath = tk.StringVar(value='No file selected')
        self.col1 = tk.StringVar()
        self.col2 = tk.StringVar()
        self.col3 = tk.StringVar()
        self.plc_ip = tk.StringVar(value=MULTICAST_GROUP)
        self.plc_port = tk.StringVar(value=str(RTU_LISTEN_PORT))
        self.listen_port = tk.StringVar(value=str(RTU_SEND_PORT))
        self.tank_selection = tk.StringVar(value=TANK_OPTIONS[0]['name'])
        self.rtu_number = tk.StringVar(value=str(TANK_OPTIONS[0]['rtu']))
        self.header_vars = {key: tk.StringVar(value=default) for key, _, default in FRAME_HEADER_FIELDS}
        self.status_text = tk.StringVar(value='Ready')
        self.is_sending = False
        self.stop_sending = False
        self.is_sample = False
        self.cal_points = None
        self.cal_filename = None
        self.cal_result = None
        self.cal_message = ''
        self.send_df = None
        self.sending_df = None
        self.sending_label = ''

        self._build_ui()
        self._apply_tank_preset()
        for key in ('density', 'tank_height'):
            self.header_vars[key].trace_add('write', lambda *_: self._recalibrate())

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 5}

        # --- Connection settings ---
        conn_frame = ttk.LabelFrame(self.root, text='PLC Connection (UDP Multicast)')
        conn_frame.pack(fill='x', **pad)

        ttk.Label(conn_frame, text='Multicast IP:').grid(row=0, column=0, sticky='e', padx=5, pady=5)
        ttk.Entry(conn_frame, textvariable=self.plc_ip, width=18).grid(row=0, column=1, padx=5)

        ttk.Label(conn_frame, text='Send Port:').grid(row=0, column=2, sticky='e', padx=5)
        ttk.Entry(conn_frame, textvariable=self.plc_port, width=8).grid(row=0, column=3, padx=5)

        ttk.Label(conn_frame, text='Listen Port:').grid(row=1, column=0, sticky='e', padx=5, pady=5)
        ttk.Entry(conn_frame, textvariable=self.listen_port, width=8).grid(row=1, column=1, sticky='w', padx=5)

        # --- Tank details ---
        header_frame = ttk.LabelFrame(self.root, text='Tank details')
        header_frame.pack(fill='x', **pad)
        header_frame.columnconfigure(1, weight=1)
        header_frame.columnconfigure(3, weight=1)

        ttk.Label(header_frame, text='Tank:').grid(
            row=0, column=0, sticky='e', padx=(10, 5), pady=3,
        )
        tank_combo = ttk.Combobox(
            header_frame,
            textvariable=self.tank_selection,
            values=[t['name'] for t in TANK_OPTIONS],
            state='readonly',
            width=28,
        )
        tank_combo.grid(row=0, column=1, sticky='ew', padx=(0, 10), pady=3)
        tank_combo.bind('<<ComboboxSelected>>', self._on_tank_selected)

        ttk.Label(header_frame, text='RTU_Number:').grid(
            row=0, column=2, sticky='e', padx=(10, 5), pady=3,
        )
        rtu_entry = ttk.Entry(header_frame, textvariable=self.rtu_number, width=8, state='readonly')
        rtu_entry.grid(row=0, column=3, sticky='w', padx=(0, 10), pady=3)

        for i, (key, label, _default) in enumerate(FRAME_HEADER_FIELDS):
            row, col = divmod(i, 2)
            base_col = col * 2
            ttk.Label(header_frame, text=label + ':').grid(
                row=row + 1, column=base_col, sticky='e', padx=(10, 5), pady=3,
            )
            ttk.Entry(header_frame, textvariable=self.header_vars[key], width=14).grid(
                row=row + 1, column=base_col + 1, sticky='ew', padx=(0, 10), pady=3,
            )

        ttk.Label(
            header_frame,
            text='Column_Size defaults to row count if left empty.',
            foreground='gray',
        ).grid(row=4, column=0, columnspan=4, sticky='w', padx=10, pady=(0, 6))

        # --- File selection ---
        file_frame = ttk.LabelFrame(self.root, text='Tank file')
        file_frame.pack(fill='x', **pad)

        ttk.Label(file_frame, textvariable=self.filepath, foreground='blue').pack(side='left', padx=10, pady=8)
        file_btn_frame = ttk.Frame(file_frame)
        file_btn_frame.pack(side='right', padx=10)
        ttk.Button(file_btn_frame, text='Load sample data', command=self.load_sample_data).pack(
            side='left', padx=(0, 5),
        )
        ttk.Button(file_btn_frame, text='Browse...', command=self.pick_file).pack(side='left')

        # --- Column mapping ---
        data_frame = ttk.LabelFrame(self.root, text='Column mapping')
        data_frame.pack(fill='x', **pad)
        data_frame.columnconfigure(1, weight=1)

        mapping_rows = (
            ('Column 1 (sent first)', self.col1),
            ('Column 2 (sent second)', self.col2),
            ('Column 3 (sent third)', self.col3),
        )
        self._col_combos = []
        for row_i, (label, var) in enumerate(mapping_rows):
            ttk.Label(data_frame, text=label + ':').grid(
                row=row_i, column=0, sticky='e', padx=(10, 5), pady=4,
            )
            combo = ttk.Combobox(
                data_frame, textvariable=var, state='disabled', width=36,
            )
            combo.grid(row=row_i, column=1, sticky='ew', padx=(0, 10), pady=4)
            combo.bind('<<ComboboxSelected>>', self._on_column_mapping_changed)
            self._col_combos.append(combo)

        self.mapping_hint = ttk.Label(
            data_frame,
            text='Load a tank file, then choose which columns to send.',
            foreground='gray',
        )
        self.mapping_hint.grid(row=3, column=0, columnspan=2, sticky='w', padx=10, pady=(0, 6))

        # Packed before the log so the button stays on screen when the log grows.
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(side='bottom', fill='x', **pad)

        self.send_btn = tk.Button(btn_frame, text='Send to PLC',
                                  command=self.start_send,
                                  bg='#4CAF50', fg='white',
                                  font=('Helvetica', 13, 'bold'),
                                  height=2)
        self.send_btn.pack(fill='x', padx=10)

        status_frame = ttk.Frame(self.root)
        status_frame.pack(side='bottom', fill='x', padx=20, pady=(0, 5))
        ttk.Label(status_frame, text='Status:').pack(side='left')
        self.status_label = ttk.Label(status_frame, textvariable=self.status_text)
        self.status_label.pack(side='left', padx=(5, 0))

        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.pack(side='bottom', fill='x', padx=20, pady=5)

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text='Log')
        log_frame.pack(fill='both', expand=True, **pad)

        self.log_box = tk.Text(log_frame, height=8, wrap='word')
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side='left', fill='both', expand=True, padx=(5, 0), pady=5)
        scrollbar.pack(side='right', fill='y', pady=5)

    # ---------- CSV load & column mapping ----------

    def _load_dataframe(self, df, source_label, is_sample=False):
        """Store full CSV, populate column selectors, build mapped send frame."""
        if df.shape[1] < 3:
            raise ValueError(f'CSV needs at least 3 columns, found {df.shape[1]}')
        self.is_sample = is_sample
        self.raw_df = df.reset_index(drop=True)
        self.filepath.set(source_label)
        self.log(f'Loaded {len(self.raw_df)} rows, {self.raw_df.shape[1]} columns from {source_label}')
        self.log(f'File columns: {", ".join(map(str, self.raw_df.columns))}')
        self._populate_column_selectors()
        if not self._refresh_mapped_dataframe(log=True):
            raise ValueError(self._column_mapping_error())

    def _column_display_names(self):
        return [str(c) for c in self.raw_df.columns]

    def _resolve_column(self, selected):
        """Map combobox display string back to the real DataFrame column label."""
        for col in self.raw_df.columns:
            if str(col) == selected:
                return col
        return None

    def _populate_column_selectors(self):
        names = self._column_display_names()
        defaults = names[:3]
        for combo, var, default in zip(self._col_combos, (self.col1, self.col2, self.col3), defaults, strict=False):
            combo['values'] = names
            combo.configure(state='readonly')
            var.set(default)
        self.mapping_hint.configure(
            text=f'{len(names)} columns in file — pick Column 1/2/3 (must be three different columns)',
        )

    def _column_mapping_error(self):
        if self.raw_df is None:
            return 'No tank file loaded.'
        c1_s, c2_s, c3_s = self.col1.get(), self.col2.get(), self.col3.get()
        if not c1_s or not c2_s or not c3_s:
            return 'Select a column for Column 1, Column 2, and Column 3.'
        resolved = {
            'Column 1': self._resolve_column(c1_s),
            'Column 2': self._resolve_column(c2_s),
            'Column 3': self._resolve_column(c3_s),
        }
        for label, col in resolved.items():
            if col is None:
                return f'{label} selection is not in the loaded file.'
        if len(set(resolved.values())) < 3:
            return 'Column 1, Column 2, and Column 3 must be three different columns.'
        return None

    def _selected_file_columns(self):
        """Return (col1, col2, col3) as real DataFrame column labels."""
        return (
            self._resolve_column(self.col1.get()),
            self._resolve_column(self.col2.get()),
            self._resolve_column(self.col3.get()),
        )

    def _refresh_mapped_dataframe(self, log=False):
        err = self._column_mapping_error()
        if err:
            self.df = None
            self._recalibrate()
            if log:
                self.log(f'Column mapping: {err}')
            return False
        c1, c2, c3 = self._selected_file_columns()
        mapped = self.raw_df[[c1, c2, c3]].copy().reset_index(drop=True)
        mapped.columns = ['COL1', 'COL2', 'COL3']

        # Coerce to numeric; multi-section CSVs may contain header rows inside the data.
        numeric = mapped.copy()
        numeric['COL1'] = pd.to_numeric(numeric['COL1'], errors='coerce')
        numeric['COL2'] = pd.to_numeric(numeric['COL2'], errors='coerce')
        numeric['COL3'] = pd.to_numeric(numeric['COL3'], errors='coerce')

        valid_mask = numeric[['COL1', 'COL2', 'COL3']].notna().all(axis=1)
        dropped = int((~valid_mask).sum())
        numeric = numeric.loc[valid_mask].copy().reset_index(drop=True)

        numeric['COL1'] = numeric['COL1'].astype(float)
        numeric['COL2'] = numeric['COL2'].astype(float)
        numeric['COL3'] = numeric['COL3'].astype(float)

        self.df = numeric
        self._recalibrate(log=log)
        if log:
            self.log(f'Mapping: [{c1}] → Column 1 | [{c2}] → Column 2 | [{c3}] → Column 3')
            if dropped:
                self.log(
                    f'Note: skipped {dropped} row(s) with non-numeric values in the selected columns '
                    f'(likely extra headers/blank lines between tables)'
                )
                bad = mapped.loc[~valid_mask, ['COL1', 'COL2', 'COL3']].head(5)
                for idx, r in bad.iterrows():
                    self.log(f'  skipped row {idx + 1}: COL1={r["COL1"]} COL2={r["COL2"]} COL3={r["COL3"]}')
            self._log_csv_preview()
        return True

    def _on_column_mapping_changed(self, _event=None):
        if self.raw_df is None:
            return
        if self._refresh_mapped_dataframe(log=True):
            self.set_status('Column mapping updated', 'blue')

    def _selected_tank(self):
        name = self.tank_selection.get()
        for tank in TANK_OPTIONS:
            if tank['name'] == name:
                return tank
        return TANK_OPTIONS[0]

    def _on_tank_selected(self, _event=None):
        self._apply_tank_preset()
        tank = self._selected_tank()
        self.log(f'Tank preset applied: {tank["name"]} (header fields remain editable)')

    def _apply_tank_preset(self):
        """Fill RTU and header fields from the selected tank's preset."""
        tank = self._selected_tank()
        self.rtu_number.set(str(tank['rtu']))
        for key, value in preset_header_fields(tank['preset']).items():
            self.header_vars[key].set(f'{value:g}')

    def _header_values(self, table=None):
        """Read and validate the 8 frame header REALs. Column_Size defaults to the sent table's row count."""
        if table is None:
            table = self.send_df if self.send_df is not None else self.df
        if table is None:
            raise ValueError('No mapped data loaded.')
        tank = self._selected_tank()
        values = [
            ('RTU_Number', tank['rtu']),
            ('Tank_Number', float(tank['tank'])),
        ]
        for key, label, _ in FRAME_HEADER_FIELDS:
            raw = self.header_vars[key].get().strip()
            if key == 'column_size' and not raw:
                raw = str(len(table))
            try:
                values.append((label, float(raw)))
            except ValueError:
                raise ValueError(f'{label} must be a number.') from None
        return values

    def _log_header_preview(self):
        self.log('--- Tank details (header REALs, indices 0..7, in combined packet) ---')
        for i, (label, value) in enumerate(self._header_values(self.sending_df)):
            self.log(f'  [{i}] {label} = {value}')

    def _log_csv_preview(self, df=None, title='Mapped data preview'):
        """Log every row’s 3-column values (mapped CSV by default)."""
        if df is None:
            df = self.df
        if df is None:
            return
        self.log(f'--- {title} ---')
        for i, row in enumerate(df.itertuples(index=False), start=1):
            v1, v2, v3 = float(row[0]), float(row[1]), float(row[2])
            self.log(
                f'  row {i}: COL1={v1}, COL2={v2}, COL3={v3}'
            )
        n = len(df)
        total_reals = NUM_HEADER_REALS + n * NUM_COLUMNS
        total_bytes = total_reals * REAL_SIZE
        self.log(
            f'--- Send plan: 1 combined packet '
            f'({NUM_HEADER_REALS} header + {n * NUM_COLUMNS} CSV = {total_reals} REALs = {total_bytes}B), '
            f'repeat every {ROUND_DELAY_SEC * 1000:.0f} ms ---'
        )
        if total_reals > PLC_BUFFER_REALS:
            self.log(
                f'WARNING: {total_reals} REALs exceeds PLC buffer of {PLC_BUFFER_REALS} '
                f'(ARRAY[0..{PLC_BUFFER_REALS - 1}]). Reduce rows or enlarge the PLC array.'
            )

    def _log_byte_distribution(self, total_rows):
        """Log byte/index breakdown for the single combined packet."""
        header_bytes = NUM_HEADER_REALS * REAL_SIZE
        col_bytes = total_rows * REAL_SIZE
        data_bytes = col_bytes * NUM_COLUMNS
        packet_bytes = header_bytes + data_bytes

        self.log('--- Byte / index distribution (single combined packet) ---')
        self.log(
            f'Total payload per round: {packet_bytes} B '
            f'({NUM_HEADER_REALS} header REALs + {total_rows * NUM_COLUMNS} CSV REALs)'
        )
        self.log(f'  Header: REAL index 0..{NUM_HEADER_REALS - 1} ({header_bytes}B)')
        for i, (label, _value) in enumerate(self._header_values(self.sending_df)):
            self.log(f'    [{i}] {label}')
        for col_i, col_label in enumerate(COLUMN_LABELS):
            start = NUM_HEADER_REALS + col_i * total_rows
            end = start + total_rows - 1
            self.log(
                f'  {col_label}: REAL index {start}..{end}, {col_bytes} B'
            )

    def load_sample_data(self):
        try:
            self._set_calibration_points([], None)
            df = pd.DataFrame(SAMPLE_CSV_ROWS)
            self._load_dataframe(df, 'sample_data (in-memory)', is_sample=True)
            self.log('Tip: use Load sample data + Send to PLC to verify all 3 columns in the log.')
        except Exception as e:
            messagebox.showerror('Error loading sample data', str(e))

    def pick_file(self):
        path = filedialog.askopenfilename(
            title='Choose tank file',
            filetypes=[
                ('Tank files', '*.xlsx *.xlsm *.csv'),
                ('Excel files', '*.xlsx *.xlsm'),
                ('CSV files', '*.csv'),
                ('All files', '*.*'),
            ],
        )
        if not path:
            return
        try:
            loaded = load_tank_file(path)
            self._set_calibration_points(loaded.points, loaded.calibration_sheet)
            self._load_dataframe(loaded.table, path.split('/')[-1])
        except Exception as e:
            messagebox.showerror('Error reading tank file', str(e))

    # ---------- Calibration ----------

    def _set_calibration_points(self, points, sheet_name):
        """Calibration points come from the second sheet of the loaded tank file."""
        self.cal_points = points or None
        self.cal_filename = sheet_name
        if sheet_name and points:
            self.log(f'Calibration points: {len(points)} from sheet {sheet_name}')

    def _header_float(self, key):
        try:
            return float(self.header_vars[key].get().strip())
        except ValueError:
            return None

    def _recalibrate(self, log=False):
        """Rebuild the table to send. A second sheet is applied in offset mode; otherwise the base table is sent."""
        self.cal_result = None
        self.send_df = None
        self.cal_message = ''
        if self.df is None:
            self.cal_message = 'Load a tank file'
            return
        if self.is_sample:
            self.send_df = self.df
            return
        mode = Mode.OFFSET if self.cal_points else Mode.NONE
        density = self._header_float('density')
        if mode is not Mode.NONE and (density is None or density <= 0):
            self.cal_message = 'Density must be a positive number to calibrate'
            return

        result = calibrate(
            self.df['COL1'].tolist(), self.df['COL2'].tolist(), self.df['COL3'].tolist(),
            self.cal_points or [], mode, density or 0, tank_height=self._header_float('tank_height'),
        )
        self.cal_result = result
        if result.levels:
            self.send_df = pd.DataFrame({'COL1': result.levels, 'COL2': result.volumes, 'COL3': result.weights})

        what = 'Calibrated' if mode is not Mode.NONE else 'Base table'
        self.cal_message = (
            f'{what} {len(result.levels)} rows · {len(result.warnings)} warnings · {len(result.errors)} errors'
        )
        if log:
            self.log(self.cal_message)

    def _confirm_calibration(self):
        """Errors block sending; warnings need confirmation. Returns True to proceed."""
        self._recalibrate()
        if self.is_sample:
            return True
        result = self.cal_result
        if result is None:
            messagebox.showerror('Calibration', self.cal_message or 'Nothing to send')
            return False
        if result.errors:
            messagebox.showerror('Calibration errors — not sending', _format_issues(result.errors))
            return False
        warnings = list(result.warnings)
        column_size = self.header_vars['column_size'].get().strip()
        if column_size:
            try:
                if float(column_size) != len(self.send_df):
                    warnings.insert(
                        0, f'Column_Size field is {column_size} but the table to send has {len(self.send_df)} rows.',
                    )
            except ValueError:
                pass
        if warnings:
            return messagebox.askyesno(
                'Calibration warnings',
                _format_issues(warnings) + '\n\nSend anyway?',
            )
        return True

    def log(self, msg):
        self.log_box.insert(tk.END, msg + '\n')
        self.log_box.see(tk.END)

    def set_status(self, msg, color=''):
        def _update():
            self.status_text.set(msg)
            if color:
                self.status_label.configure(foreground=color)
        self.root.after(0, _update)

    def start_send(self):
        if self.is_sending:
            self.stop_sending = True
            self.set_status('Stopping after current round…', 'darkorange')
            return
        if self.raw_df is None:
            messagebox.showwarning('No file', 'Pick a tank file first.')
            return
        if not self._refresh_mapped_dataframe():
            messagebox.showwarning('Column mapping', self._column_mapping_error())
            return
        try:
            self._header_values()
        except ValueError as e:
            messagebox.showwarning('Tank details', str(e))
            return
        if not self._confirm_calibration():
            return
        self.sending_df = self.send_df
        self.sending_label = (
            'sample data' if self.is_sample
            else f'calibration sheet {self.cal_filename}' if self.cal_points
            else 'base table'
        )
        self.is_sending = True
        self.stop_sending = False
        self.send_btn.config(text='Stop', bg='#f44336')
        self.set_status('Sending (loop)…', 'blue')
        threading.Thread(target=self._send_worker, daemon=True).start()

    def _send_one_packet(self, send_sock, group, send_port, step_no, total_steps, label, packet):
        """Send one UDP packet. Returns sent|failed."""
        try:
            send_sock.sendto(packet, (group, send_port))
            return 'sent'
        except Exception as e:
            self.log(f'[{step_no}/{total_steps}] FAILED {label}: {e}')
            return 'failed'

    # ---------- Network (CODESYS RTU MULTICAST — single combined packet) ----------

    def _send_one_round(self, send_sock, group, send_port, round_no):
        """Send ONE combined UDP packet: 8 header REALs, then CSV column-major."""
        total_rows = len(self.sending_df)
        total_steps = 1
        self.progress['maximum'] = total_steps
        self.progress['value'] = 0
        err_count = 0

        self.log(f'--- Round {round_no} ---')

        try:
            header_items = self._header_values(self.sending_df)
            packet = build_combined_packet(header_items, self.sending_df)

            expected_reals = NUM_HEADER_REALS + total_rows * NUM_COLUMNS
            expected_bytes = expected_reals * REAL_SIZE
            if len(packet) != expected_bytes:
                self.log(
                    f'[1/1] WARNING: packet is {len(packet)}B, expected {expected_bytes}B'
                )

            self.progress['value'] = 1
            result = self._send_one_packet(
                send_sock, group, send_port,
                1, total_steps,
                f'combined ({NUM_HEADER_REALS} header + {total_rows * NUM_COLUMNS} CSV '
                f'= {expected_reals} REALs, {len(packet)}B)',
                packet,
            )
            if result == 'failed':
                err_count += 1
        except Exception as e:
            self.log(f'Round {round_no} FAILED: {e}')
            err_count += 1

        self.log(
            f'--- Round {round_no} complete: 1 combined packet '
            f'({NUM_HEADER_REALS} header + {total_rows * NUM_COLUMNS} CSV REALs), {err_count} failed ---'
        )
        return err_count, total_steps

    def _send_worker(self):
        send_sock = None
        try:
            group = self.plc_ip.get().strip()
            send_port = int(self.plc_port.get())

            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

            total_rows = len(self.sending_df)
            self.log(f'--- Starting send loop to {group}:{send_port} ---')
            self.log(f'Data: {self.sending_label}')
            self.log(packet_format_summary())
            self.log(
                'Order (single packet): [RTU_Number, Tank_Number, Column_Size, '
                'Sensor_Min_bar, Sensor_Max_bar, Density, Dead_Volume, Tank_Height, '
                'COL1…, COL2…, COL3…]'
            )
            self.log(f'Loop: repeat full send every {ROUND_DELAY_SEC * 1000:.0f} ms (click Stop to end)')
            self._log_header_preview()
            self._log_csv_preview(self.sending_df, 'Data to send')
            self.log('PLC reply verification: disabled (fire-and-forget)')
            self._log_byte_distribution(total_rows)

            round_no = 0
            total_err = 0
            total_packets = 0

            while not self.stop_sending:
                round_no += 1
                err_count, steps = self._send_one_round(
                    send_sock, group, send_port, round_no,
                )
                total_err += err_count
                total_packets += steps
                if self.stop_sending:
                    break
                self.log(f'Waiting {ROUND_DELAY_SEC * 1000:.0f} ms before next round…')
                time.sleep(ROUND_DELAY_SEC)

            summary = f'Stopped: {round_no} round(s), {total_packets} packets, {total_err} failed'
            self.log(f'--- {summary} ---')
            color = 'red' if total_err else 'green'
            self.set_status(summary, color)

        except Exception as e:
            msg = str(e)
            self.log(f'FATAL: {msg}')
            self.set_status(f'Failed: {msg}', 'red')
            self.root.after(0, lambda: messagebox.showerror('Send failed', msg))
        finally:
            if send_sock:
                send_sock.close()
            self.is_sending = False
            self.stop_sending = False
            self.root.after(
                0,
                lambda: self.send_btn.config(state='normal', text='Send to PLC', bg='#4CAF50'),
            )


def _format_issues(issues):
    shown = '\n'.join(f'• {i}' for i in issues[:MAX_LISTED_ISSUES])
    if len(issues) > MAX_LISTED_ISSUES:
        shown += f'\n… and {len(issues) - MAX_LISTED_ISSUES} more'
    return shown


if __name__ == '__main__':
    root = tk.Tk()
    CSVtoPLCApp(root)
    root.mainloop()