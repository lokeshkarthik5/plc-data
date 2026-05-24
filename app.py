"""
CSV to PLC over UDP
Run: python3 app.py
"""

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
        ttk.Label(data_frame, text='Column 2 → PAO value (float32)').grid(row=1, column=0, sticky='w', padx=10, pady=2)
        ttk.Label(data_frame, text='Column 3 → DAO value (float32)').grid(row=2, column=0, sticky='w', padx=10, pady=2)

        self.col1_name = tk.StringVar(value='—')
        self.col2_name = tk.StringVar(value='—')
        self.col3_name = tk.StringVar(value='—')
        ttk.Label(data_frame, textvariable=self.col1_name, foreground='gray').grid(row=0, column=1, sticky='w', padx=5)
        ttk.Label(data_frame, textvariable=self.col2_name, foreground='gray').grid(row=1, column=1, sticky='w', padx=5)
        ttk.Label(data_frame, textvariable=self.col3_name, foreground='gray').grid(row=2, column=1, sticky='w', padx=5)

        # --- Column data preview ---
        preview_frame = ttk.LabelFrame(self.root, text='Column data')
        preview_frame.pack(fill='both', expand=True, **pad)

        tree_container = ttk.Frame(preview_frame)
        tree_container.pack(fill='both', expand=True, padx=5, pady=5)

        self.data_tree = ttk.Treeview(tree_container, show='headings', height=10)
        tree_y = ttk.Scrollbar(tree_container, orient='vertical', command=self.data_tree.yview)
        tree_x = ttk.Scrollbar(tree_container, orient='horizontal', command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)

        self.data_tree.grid(row=0, column=0, sticky='nsew')
        tree_y.grid(row=0, column=1, sticky='ns')
        tree_x.grid(row=1, column=0, sticky='ew')
        tree_container.rowconfigure(0, weight=1)
        tree_container.columnconfigure(0, weight=1)

        # --- Action button ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill='x', **pad)

        self.send_btn = tk.Button(btn_frame, text='Send to PLC',
                                  command=self.start_send,
                                  bg='#4CAF50', fg='white',
                                  font=('Helvetica', 13, 'bold'),
                                  height=2)
        self.send_btn.pack(fill='x', padx=10)

        # --- Progress bar ---
        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.pack(fill='x', padx=20, pady=5)

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text='Log')
        log_frame.pack(fill='both', expand=True, **pad)

        self.log_box = tk.Text(log_frame, height=8, wrap='word')
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self.log_box.pack(side='left', fill='both', expand=True, padx=(5, 0), pady=5)
        scrollbar.pack(side='right', fill='y', pady=5)

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
            self._refresh_data_table()
            self.log(f'Loaded {len(self.df)} rows from {path}')
            self.log(f'Using columns: {", ".join(cols)}')
        except Exception as e:
            messagebox.showerror('Error reading CSV', str(e))

    def _refresh_data_table(self):
        for item in self.data_tree.get_children():
            self.data_tree.delete(item)
        if self.df is None:
            return
        cols = [str(c) for c in self.df.columns]
        self.data_tree['columns'] = cols
        for col in cols:
            self.data_tree.heading(col, text=col)
            self.data_tree.column(col, width=140, minwidth=80, anchor='w')
        for row in self.df.itertuples(index=False):
            self.data_tree.insert('', tk.END, values=tuple(str(v) for v in row))

    def log(self, msg):
        self.log_box.insert(tk.END, msg + '\n')
        self.log_box.see(tk.END)

    def start_send(self):
        if self.is_sending:
            return
        if self.df is None:
            messagebox.showwarning('No file', 'Pick a CSV first.')
            return
        # Run network work in a thread so UI stays responsive
        self.is_sending = True
        self.send_btn.config(state='disabled', text='Sending...')
        thread = threading.Thread(target=self._send_worker, daemon=True).start()

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
                recv_sock.settimeout(0.3)
                reply_enabled = True
            except OSError as e:
                self.log(f'(reply listener disabled: {e})')
                reply_enabled = False

            total = len(self.df)
            self.progress['maximum'] = total
            self.log(f'--- Starting send to {group}:{send_port} (RTU={RTU_NUMBER}) ---')

            for i, row in enumerate(self.df.itertuples(index=False)):
                try:
                    channel = int(row[0])
                    pao_val = float(row[1])
                    dao_val = float(row[2])

                    # One row = one PAO write + one DAO write on the same channel
                    packet = build_scada_packet(
                        rtu_no=RTU_NUMBER,
                        pdo_rows=[],
                        ddo_rows=[],
                        pao_rows=[(channel, pao_val)],
                        dao_rows=[(channel, dao_val)],
                    )

                    send_sock.sendto(packet, (group, send_port))
                    self.log(f'[{i+1}/{total}] ch={channel} PAO={pao_val} DAO={dao_val} '
                             f'-> {len(packet)} bytes: {packet.hex()}')

                    # Optional: brief listen for RTU reply
                    if reply_enabled:
                        try:
                            data, addr = recv_sock.recvfrom(4096)
                            self.log(f'    reply from {addr}: {len(data)}B first32={data[:32].hex()}')
                        except socket.timeout:
                            pass

                except Exception as e:
                    self.log(f'[{i+1}/{total}] ERROR: {e}')

                self.progress['value'] = i + 1
                time.sleep(0.05)  # small pacing gap

            self.log('--- Done ---')

        except Exception as e:
            self.log(f'FATAL: {e}')
            messagebox.showerror('Send failed', str(e))
        finally:
            if send_sock:
                send_sock.close()
            if recv_sock:
                recv_sock.close()
            self.is_sending = False
            self.send_btn.config(state='normal', text='Send to PLC')


if __name__ == '__main__':
    root = tk.Tk()
    CSVtoPLCApp(root)
    root.mainloop()