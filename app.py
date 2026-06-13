import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import socket
import struct
import threading
import time
import pandas as pd


# ---- UDP multicast defaults ----
MULTICAST_GROUP = '239.255.43.21'
RTU_LISTEN_PORT = 4001     # PLC listen port (SCADA -> RTU)
RTU_SEND_PORT = 9001      # PLC send port (RTU -> SCADA)
REAL_SIZE = 4               # One float32 (real), little-endian
ROUND_DELAY_SEC = 0.1       # Pause between complete send rounds


def build_column_batch(values):
    """Pack all row values into one UDP payload: N × float32 LE, contiguous."""
    floats = [float(v) for v in values]
    return struct.pack(f'<{len(floats)}f', *floats)


def packet_format_summary():
    return (
        'UDP payload: one packet per column, N×4B float32 (real) contiguous '
        '(bytes 0-3=row1, 4-7=row2, …), little-endian'
    )

# Each send pass: (df_col_index, label)
SEND_PASSES = (
    (0, 'COL1'),
    (1, 'COL2'),
    (2, 'COL3'),
)
SAMPLE_CSV_ROWS = (
    {'Channel': 101, 'PAO': 1.11, 'DAO': 9.91},
    {'Channel': 102, 'PAO': 2.22, 'DAO': 8.82},
    {'Channel': 103, 'PAO': 3.33, 'DAO': 7.73},
)


def describe_batched_packet(packet, max_preview=3):
    """Byte size, value preview, and hex for a batched float32 column packet."""
    size = len(packet)
    if size == 0 or size % REAL_SIZE != 0:
        return f'{size}B, format=invalid (not a multiple of 4B), hex={packet.hex(" ")}'
    n = size // REAL_SIZE
    vals = struct.unpack(f'<{n}f', packet)
    preview = ', '.join(str(v) for v in vals[:max_preview])
    if n > max_preview:
        preview += f', … ({n} reals total)'
    hex_len = min(16, size)
    hex_part = packet[:hex_len].hex(' ')
    if size > hex_len:
        hex_part += ' …'
    return f'{size}B, format={n}×float32 LE, vals=[{preview}], hex={hex_part}'


class CSVtoPLCApp:
    def __init__(self, root):
        self.root = root
        self.root.title('CSV to PLC')
        self.root.geometry('700x750')

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
        self.status_text = tk.StringVar(value='Ready')
        self.is_sending = False
        self.stop_sending = False

        self._build_ui()

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

        # --- File selection ---
        file_frame = ttk.LabelFrame(self.root, text='CSV File')
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
            text='Load a CSV, then choose which file columns to use.',
            foreground='gray',
        )
        self.mapping_hint.grid(row=3, column=0, columnspan=2, sticky='w', padx=10, pady=(0, 6))

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text='Log')
        log_frame.pack(fill='both', expand=True, **pad)

        self.log_box = tk.Text(log_frame, height=16, wrap='word')
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side='left', fill='both', expand=True, padx=(5, 0), pady=5)
        scrollbar.pack(side='right', fill='y', pady=5)

        # --- Action button ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', **pad)

        self.send_btn = tk.Button(btn_frame, text='Send to PLC',
                                  command=self.start_send,
                                  bg='#4CAF50', fg='white',
                                  font=('Helvetica', 13, 'bold'),
                                  height=2)
        self.send_btn.pack(fill='x', padx=10)

        # --- Send status ---
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill='x', padx=20, pady=(0, 5))
        ttk.Label(status_frame, text='Status:').pack(side='left')
        self.status_label = ttk.Label(status_frame, textvariable=self.status_text)
        self.status_label.pack(side='left', padx=(5, 0))

        # --- Progress bar ---
        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.pack(fill='x', padx=20, pady=5)

    # ---------- CSV load & column mapping ----------

    def _load_dataframe(self, df, source_label):
        """Store full CSV, populate column selectors, build mapped send frame."""
        if df.shape[1] < 3:
            raise ValueError(f'CSV needs at least 3 columns, found {df.shape[1]}')
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
        for combo, var, default in zip(self._col_combos, (self.col1, self.col2, self.col3), defaults):
            combo['values'] = names
            combo.configure(state='readonly')
            var.set(default)
        self.mapping_hint.configure(
            text=f'{len(names)} columns in file — pick Column 1/2/3 (must be three different columns)',
        )

    def _column_mapping_error(self):
        if self.raw_df is None:
            return 'No CSV loaded.'
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

    def _log_csv_preview(self):
        """Log every row’s mapped 3-column values."""
        if self.df is None:
            return
        self.log('--- Mapped data preview ---')
        for i, row in enumerate(self.df.itertuples(index=False), start=1):
            v1, v2, v3 = float(row[0]), float(row[1]), float(row[2])
            self.log(
                f'  row {i}: COL1={v1}, COL2={v2}, COL3={v3}'
            )
        n = len(self.df)
        batch_bytes = n * REAL_SIZE
        self.log(
            f'--- Send plan: COL1 then COL2 then COL3, one batched UDP each '
            f'({n} reals × 4B = {batch_bytes}B per column), '
            f'repeat every {ROUND_DELAY_SEC * 1000:.0f} ms ---'
        )

    def _log_byte_distribution(self, total_rows):
        """Log byte breakdown in the on-screen log."""
        batch_bytes = total_rows * REAL_SIZE
        round_bytes = batch_bytes * len(SEND_PASSES)

        self.log('--- Byte distribution ---')
        self.log(
            f'Total payload per round: {round_bytes} B '
            f'({len(SEND_PASSES)} batched packets × {batch_bytes}B each)'
        )
        for col_label in ('COL1', 'COL2', 'COL3'):
            self.log(
                f'  {col_label}: 1 packet, {batch_bytes} B '
                f'({total_rows} reals × {REAL_SIZE}B, bytes 0-3=row1, 4-7=row2, …)'
            )

    def load_sample_data(self):
        try:
            df = pd.DataFrame(SAMPLE_CSV_ROWS)
            self._load_dataframe(df, 'sample_data (in-memory)')
            self.log('Tip: use Load sample data + Send to PLC to verify all 3 columns in the log.')
        except Exception as e:
            messagebox.showerror('Error loading sample data', str(e))

    def pick_file(self):
        path = filedialog.askopenfilename(
            title='Choose CSV',
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')]
        )
        if not path:
            return
        try:
            self._load_dataframe(pd.read_csv(path), path.split('/')[-1])
        except Exception as e:
            messagebox.showerror('Error reading CSV', str(e))

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
            messagebox.showwarning('No file', 'Pick a CSV first.')
            return
        if not self._refresh_mapped_dataframe():
            messagebox.showwarning('Column mapping', self._column_mapping_error())
            return
        self.is_sending = True
        self.stop_sending = False
        self.send_btn.config(text='Stop', bg='#f44336')
        self.set_status('Sending (loop)…', 'blue')
        threading.Thread(target=self._send_worker, daemon=True).start()

    def _send_one_packet(self, send_sock, group, send_port, step_no, total_steps, label, packet):
        """Send one batched UDP packet. Returns sent|failed."""
        try:
            send_sock.sendto(packet, (group, send_port))
            return 'sent'
        except Exception as e:
            self.log(f'[{step_no}/{total_steps}] FAILED {label}: {e}')
            return 'failed'

    # ---------- Network (REWRITTEN FOR CODESYS RTU MULTICAST) ----------

    def _send_one_round(self, send_sock, group, send_port, round_no, verbose=False):
        """Send one full COL1 → COL2 → COL3 pass (one batched UDP per column)."""
        total_rows = len(self.df)
        total_steps = len(SEND_PASSES)
        self.progress['maximum'] = total_steps
        self.progress['value'] = 0
        err_count = 0
        step_no = 0

        self.log(f'--- Round {round_no} ---')

        def tally(result):
            nonlocal err_count
            if result == 'failed':
                err_count += 1

        def advance():
            nonlocal step_no
            step_no += 1
            self.progress['value'] = step_no

        for pass_no, (df_col, col_label) in enumerate(SEND_PASSES, start=1):
            if self.stop_sending:
                break
            batch_bytes = total_rows * REAL_SIZE
            self.log(
                f'--- Pass {pass_no}: send {col_label} as one batch '
                f'({total_rows} reals, {batch_bytes}B) ---'
            )

            try:
                values = [float(v) for v in self.df.iloc[:, df_col]]
                packet = build_column_batch(values)
                if verbose:
                    self.log(f'  Batch preview: {describe_batched_packet(packet)}')

                advance()
                result = self._send_one_packet(
                    send_sock, group, send_port,
                    step_no, total_steps,
                    f'{col_label} batch ({batch_bytes}B)', packet,
                )
                tally(result)
                time.sleep(0.05)
            except Exception as e:
                self.log(f'[{step_no}/{total_steps}] FAILED pass {pass_no} {col_label}: {e}')
                tally('failed')

        self.log(f'--- Round {round_no} complete: {total_steps} batched packets, {err_count} failed ---')
        return err_count, total_steps

    def _send_worker(self):
        send_sock = None
        try:
            group = self.plc_ip.get().strip()
            send_port = int(self.plc_port.get())

            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

            total_rows = len(self.df)
            self.log(f'--- Starting send loop to {group}:{send_port} ---')
            self.log(packet_format_summary())
            self.log('Order: COL1 → COL2 → COL3 (one batched UDP per column, N×4B contiguous)')
            self.log(f'Loop: repeat full send every {ROUND_DELAY_SEC * 1000:.0f} ms (click Stop to end)')
            self._log_csv_preview()
            self.log('PLC reply verification: disabled (fire-and-forget)')
            self._log_byte_distribution(total_rows)

            round_no = 0
            total_err = 0
            total_packets = 0

            while not self.stop_sending:
                round_no += 1
                err_count, steps = self._send_one_round(
                    send_sock, group, send_port, round_no, verbose=(round_no == 1),
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
            self.log(f'FATAL: {e}')
            self.set_status(f'Failed: {e}', 'red')
            self.root.after(0, lambda: messagebox.showerror('Send failed', str(e)))
        finally:
            if send_sock:
                send_sock.close()
            self.is_sending = False
            self.stop_sending = False
            self.root.after(
                0,
                lambda: self.send_btn.config(state='normal', text='Send to PLC', bg='#4CAF50'),
            )


if __name__ == '__main__':
    root = tk.Tk()
    CSVtoPLCApp(root)
    root.mainloop()