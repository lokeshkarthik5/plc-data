import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import socket
import struct
import threading
import time
import pandas as pd


# ---- RTU protocol defaults (from CODESYS code) ----
MULTICAST_GROUP = '239.255.43.21'
RTU_LISTEN_PORT = 4001     # PLC listens here (SCADA -> RTU)
RTU_SEND_PORT = 9001       # PLC sends from here (RTU -> SCADA)
RTU_NUMBER = 1             # Must match the IF RTU_NO = 1 gate in PLC code
REPLY_TIMEOUT_SEC = 0.5    # How long to wait for an RTU reply after each send
HEADER_SIZE = 10           # RTU_NO + 4 x uint16 counts (little-endian)


def parse_rtu_header(data):
    """Parse the 10-byte RTU packet header. Returns None if too short."""
    if len(data) < HEADER_SIZE:
        return None
    rtu_no, pdo_c, ddo_c, pao_c, dao_c = struct.unpack('<HHHHH', data[:HEADER_SIZE])
    return {
        'rtu_no': rtu_no,
        'pdo_count': pdo_c,
        'ddo_count': ddo_c,
        'pao_count': pao_c,
        'dao_count': dao_c,
    }


def drain_socket(sock):
    """Discard any queued UDP packets before a new send."""
    sock.setblocking(False)
    try:
        while True:
            sock.recvfrom(4096)
    except BlockingIOError:
        pass
    finally:
        sock.setblocking(True)


def wait_for_rtu_reply(sock, expected_rtu, timeout_sec):
    """
    Wait for a UDP reply whose header RTU_NO matches expected_rtu.
    Returns (ok, header_dict, raw_bytes, sender_addr).
    """
    deadline = time.monotonic() + timeout_sec
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, None, None, None
        sock.settimeout(remaining)
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            return False, None, None, None
        header = parse_rtu_header(data)
        if header is None:
            continue
        if header['rtu_no'] == expected_rtu:
            return True, header, data, addr


def build_scada_packet(rtu_no, pdo_rows, ddo_rows, pao_rows, dao_rows):
    """
    Build the SCADA -> RTU packet per the CODESYS decode logic.
    All multi-byte fields are little-endian.

    Layout:
      Header (10 bytes): RTU_NO, PDO_count, DDO_count, PAO_count, DAO_count  (all uint16 LE)
      PDO block: [ch_no x N as uint16 LE][value x N as uint8]
      DDO block: [ch_no x N as uint16 LE][value x N as uint8]
      PAO block: [ch_no x N as uint16 LE][value x N as float32 LE]
      DAO block: [ch_no x N as uint16 LE][value x N as float32 LE]
    """
    header = struct.pack('<HHHHH',
                         rtu_no,
                         len(pdo_rows),
                         len(ddo_rows),
                         len(pao_rows),
                         len(dao_rows))

    pdo_ch  = b''.join(struct.pack('<H', ch) for ch, _ in pdo_rows)
    pdo_val = b''.join(struct.pack('<B', int(v) & 0xFF) for _, v in pdo_rows)

    ddo_ch  = b''.join(struct.pack('<H', ch) for ch, _ in ddo_rows)
    ddo_val = b''.join(struct.pack('<B', int(v) & 0xFF) for _, v in ddo_rows)

    pao_ch  = b''.join(struct.pack('<H', ch) for ch, _ in pao_rows)
    pao_val = b''.join(struct.pack('<f', float(v)) for _, v in pao_rows)

    dao_ch  = b''.join(struct.pack('<H', ch) for ch, _ in dao_rows)
    dao_val = b''.join(struct.pack('<f', float(v)) for _, v in dao_rows)

    return (header
            + pdo_ch + pdo_val
            + ddo_ch + ddo_val
            + pao_ch + pao_val
            + dao_ch + dao_val)


def column_flag(prefix, index):
    """Build a row flag like $s1 or $e2."""
    return f'${prefix}{index}'.encode('ascii')


# Each data column pass: (csv_col_index, label, build_rows_fn)
SEND_COLUMNS = (
    (2, 'PAO', lambda channel, val: ([(channel, val)], [])),
    (3, 'DAO', lambda channel, val: ([], [(channel, val)])),
)


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

        self.df = None
        self.filepath = tk.StringVar(value='No file selected')
        self.plc_ip = tk.StringVar(value=MULTICAST_GROUP)
        self.plc_port = tk.StringVar(value=str(RTU_LISTEN_PORT))
        self.listen_port = tk.StringVar(value=str(RTU_SEND_PORT))
        self.status_text = tk.StringVar(value='Ready')
        self.is_sending = False

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
        ttk.Button(file_frame, text='Browse...', command=self.pick_file).pack(side='right', padx=10)

        # --- Data layout (first 3 CSV columns only) ---
        data_frame = ttk.LabelFrame(self.root, text='Data layout (uses first 3 CSV columns only)')
        data_frame.pack(fill='x', **pad)

        ttk.Label(data_frame, text='Column 1 → Channel number (uint16)').grid(row=0, column=0, sticky='w', padx=10, pady=2)
        ttk.Label(data_frame, text='Column 2 → PAO value (float32, sent first)').grid(row=1, column=0, sticky='w', padx=10, pady=2)
        ttk.Label(data_frame, text='Column 3 → DAO value (float32, sent second)').grid(row=2, column=0, sticky='w', padx=10, pady=2)

        self.col1_name = tk.StringVar(value='—')
        self.col2_name = tk.StringVar(value='—')
        self.col3_name = tk.StringVar(value='—')
        ttk.Label(data_frame, textvariable=self.col1_name, foreground='gray').grid(row=0, column=1, sticky='w', padx=5)
        ttk.Label(data_frame, textvariable=self.col2_name, foreground='gray').grid(row=1, column=1, sticky='w', padx=5)
        ttk.Label(data_frame, textvariable=self.col3_name, foreground='gray').grid(row=2, column=1, sticky='w', padx=5)

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

    # ---------- Actions (UNCHANGED CSV LOGIC) ----------

    def pick_file(self):
        path = filedialog.askopenfilename(
            title='Choose CSV',
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')]
        )
        if not path:
            return
        try:
            raw = pd.read_csv(path)
            if raw.shape[1] < 3:
                raise ValueError(f'CSV needs at least 3 columns, found {raw.shape[1]}')
            self.df = raw.iloc[:, :3].reset_index(drop=True)
            self.filepath.set(path.split('/')[-1])
            cols = list(self.df.columns)
            self.col1_name.set(cols[0])
            self.col2_name.set(cols[1])
            self.col3_name.set(cols[2])
            self.log(f'Loaded {len(self.df)} rows from {path}')
            self.log(f'Using columns: {", ".join(cols)}')
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
            return
        if self.df is None:
            messagebox.showwarning('No file', 'Pick a CSV first.')
            return
        # Run network work in a thread so UI stays responsive
        self.is_sending = True
        self.send_btn.config(state='disabled', text='Sending...')
        self.set_status('Sending…', 'blue')
        threading.Thread(target=self._send_worker, daemon=True).start()

    def _send_flag(self, send_sock, group, send_port, flag_bytes, label):
        """Send a start/end flag packet ($sN / $eN)."""
        send_sock.sendto(flag_bytes, (group, send_port))
        self.log(f'  FLAG {label} -> {flag_bytes.decode("ascii")}')

    def _send_one_packet(self, send_sock, recv_sock, group, send_port, reply_enabled,
                         step_no, total_steps, label, channel, value, packet):
        """Send one UDP packet and optionally wait for RTU reply. Returns ok|unconfirmed|sent|failed."""
        try:
            if reply_enabled:
                drain_socket(recv_sock)
            send_sock.sendto(packet, (group, send_port))

            if not reply_enabled:
                self.log(f'[{step_no}/{total_steps}] SENT {label} ch={channel} val={value} '
                         f'(not verified — no reply listener)')
                return 'sent'

            got_reply, hdr, data, addr = wait_for_rtu_reply(
                recv_sock, RTU_NUMBER, REPLY_TIMEOUT_SEC
            )
            if got_reply:
                self.log(
                    f'[{step_no}/{total_steps}] OK {label} ch={channel} val={value} — '
                    f'PLC replied from {addr[0]}:{addr[1]} ({len(data)}B, RTU={hdr["rtu_no"]}, '
                    f'PDO={hdr["pdo_count"]} DDO={hdr["ddo_count"]} '
                    f'PAO={hdr["pao_count"]} DAO={hdr["dao_count"]})'
                )
                return 'ok'
            self.log(
                f'[{step_no}/{total_steps}] UNCONFIRMED {label} ch={channel} val={value} — '
                f'sent {len(packet)}B, no RTU reply within {REPLY_TIMEOUT_SEC}s'
            )
            return 'unconfirmed'
        except Exception as e:
            self.log(f'[{step_no}/{total_steps}] FAILED {label} ch={channel}: {e}')
            return 'failed'

    # ---------- Network (REWRITTEN FOR CODESYS RTU MULTICAST) ----------

    def _send_worker(self):
        send_sock = None
        recv_sock = None
        try:
            group = self.plc_ip.get().strip()
            send_port = int(self.plc_port.get())
            listen_port = int(self.listen_port.get())

            # --- Sender socket: UDP multicast ---
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

            # --- Receiver socket: join the multicast group on listen_port ---
            recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                recv_sock.bind(('', listen_port))
                mreq = struct.pack('4sl', socket.inet_aton(group), socket.INADDR_ANY)
                recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                reply_enabled = True
            except OSError as e:
                self.log(f'(reply listener disabled: {e})')
                reply_enabled = False

            total_rows = len(self.df)
            # Per row: start flag + data + end flag, for each data column pass
            total_steps = total_rows * len(SEND_COLUMNS) * 3
            self.progress['maximum'] = total_steps
            ok_count = 0
            no_reply_count = 0
            err_count = 0
            step_no = 0

            self.log(f'--- Starting send to {group}:{send_port} (RTU={RTU_NUMBER}) ---')
            self.log('Order: all PAO (column 2) first, then all DAO (column 3); '
                     'each row wrapped with $sN / $eN flags')
            if reply_enabled:
                self.log(f'Listening for PLC replies on port {listen_port} '
                         f'(timeout {REPLY_TIMEOUT_SEC}s per packet)')
            else:
                self.log('Cannot verify PLC acceptance (reply listener unavailable)')

            def tally(result):
                nonlocal ok_count, no_reply_count, err_count
                if result == 'ok' or result == 'sent':
                    ok_count += 1
                elif result == 'unconfirmed':
                    no_reply_count += 1
                elif result == 'failed':
                    err_count += 1

            def advance():
                nonlocal step_no
                step_no += 1
                self.progress['value'] = step_no
                time.sleep(0.05)

            for pass_no, (csv_col, col_label, build_rows) in enumerate(SEND_COLUMNS, start=1):
                self.log(f'--- Pass {pass_no}: {col_label} (column {csv_col}) '
                         f'— $sN / $eN per row (N = 1..{total_rows}) ---')
                for i, row in enumerate(self.df.itertuples(index=False)):
                    row_no = i + 1
                    start_flag = column_flag('s', row_no)
                    end_flag = column_flag('e', row_no)

                    try:
                        advance()
                        self._send_flag(
                            send_sock, group, send_port, start_flag,
                            f'{col_label} row {row_no} start',
                        )

                        channel = int(row[0])
                        value = float(row[csv_col - 1])
                        pao_rows, dao_rows = build_rows(channel, value)
                        packet = build_scada_packet(
                            rtu_no=RTU_NUMBER,
                            pdo_rows=[],
                            ddo_rows=[],
                            pao_rows=pao_rows,
                            dao_rows=dao_rows,
                        )
                        advance()
                        result = self._send_one_packet(
                            send_sock, recv_sock, group, send_port, reply_enabled,
                            step_no, total_steps,
                            f'{col_label} row {row_no}', channel, value, packet,
                        )
                        tally(result)

                        advance()
                        self._send_flag(
                            send_sock, group, send_port, end_flag,
                            f'{col_label} row {row_no} end',
                        )
                    except Exception as e:
                        self.log(f'[{step_no}/{total_steps}] FAILED {col_label} row {row_no}: {e}')
                        tally('failed')

            if reply_enabled:
                summary = (f'Done: {ok_count} confirmed, {no_reply_count} unconfirmed, '
                           f'{err_count} failed (of {total_steps} packets)')
            else:
                summary = f'Done: {total_steps} sent (PLC reply check disabled)'
            self.log(f'--- {summary} ---')

            if not reply_enabled:
                color = 'darkorange'
            elif err_count or (no_reply_count == total_steps):
                color = 'red'
            elif no_reply_count:
                color = 'darkorange'
            elif ok_count == total_steps:
                color = 'green'
            else:
                color = 'green'
            self.set_status(summary, color)

            if reply_enabled and (err_count or no_reply_count):
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        'Send finished with issues',
                        summary + '\n\n'
                        'OK = PLC sent a reply on the listen port.\n'
                        'Unconfirmed = packet was sent but no matching RTU reply was received.\n'
                        'Failed = local error building or sending a packet.',
                    ),
                )

        except Exception as e:
            self.log(f'FATAL: {e}')
            self.set_status(f'Failed: {e}', 'red')
            self.root.after(0, lambda: messagebox.showerror('Send failed', str(e)))
        finally:
            if send_sock:
                send_sock.close()
            if recv_sock:
                recv_sock.close()
            self.is_sending = False
            self.root.after(0, lambda: self.send_btn.config(state='normal', text='Send to PLC'))


if __name__ == '__main__':
    root = tk.Tk()
    CSVtoPLCApp(root)
    root.mainloop()