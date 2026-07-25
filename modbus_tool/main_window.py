"""Main application window (PySide6).

Layout:
  * Left  : connection settings (TCP / Serial switch) + PC network info
  * Right : query builder (function, address, count, format) + poll controls
  * Center-bottom : results table and an activity log

All Modbus I/O is delegated to ModbusWorker on a background thread; this class
only builds requests and renders results.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QPushButton, QGroupBox, QFormLayout, QVBoxLayout,
    QHBoxLayout, QGridLayout, QStackedWidget, QRadioButton, QButtonGroup,
    QTableWidget, QTableWidgetItem, QPlainTextEdit, QHeaderView, QMessageBox,
    QTabWidget, QProgressBar, QFileDialog,
)
from PySide6.QtGui import QColor

from serial.tools import list_ports

from .modbus_client import (
    TcpConfig, SerialConfig, DataKind,
    READ_FUNCTIONS, WRITE_FUNCTIONS, ALL_FUNCTIONS, READ_HOLDING,
)
from .formatting import (
    DisplayFormat, decode_registers, decode_bits, is_wide, WORD_ORDERS,
)
from . import net_info
from . import discovery
from .ai import profile_store
from .ai.providers import AnthropicProvider, OpenAICompatProvider, DEFAULT_ANTHROPIC_MODEL
from .worker import (
    ModbusWorker, ReadRequest, WriteRequest, PollRequest,
    SlaveScanRequest, RegisterScanRequest, ExtractRequest, ValidateRequest,
)

MAX_REGISTERS = 25


class MainWindow(QMainWindow):
    # signals -> worker (queued across the thread boundary)
    sig_connect = Signal(object)
    sig_disconnect = Signal()
    sig_read = Signal(object)
    sig_write = Signal(object)
    sig_start_poll = Signal(object)
    sig_stop_poll = Signal()
    sig_scan_slaves = Signal(object)
    sig_scan_registers = Signal(object)
    sig_extract = Signal(object)
    sig_validate = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ModbusTool - Modbus Master")
        self.resize(1120, 760)

        self._connected = False
        self._polling = False
        self._scanning = False

        # discovery state
        self._slave_rows: list = []          # SlaveResult per table row
        self._region_rows: list = []         # RegionResult per table row
        self._current_suggestions: list = []  # FormatSuggestion for selected region
        self._suggest_ctx = None             # (unit, func, address) of selected region

        # register-map state
        self._profiles: list = []            # list[RegisterProfile]
        self._busy_ai = False

        self._build_ui()
        self._start_worker()
        self._refresh_serial_ports()
        self._refresh_net_info()
        self._on_function_changed()
        self._update_connection_state(False)

    # ================================================================= #
    # Worker / threading
    # ================================================================= #
    def _start_worker(self):
        self._thread = QThread(self)
        self._worker = ModbusWorker()
        self._worker.moveToThread(self._thread)

        # GUI -> worker
        self.sig_connect.connect(self._worker.do_connect)
        self.sig_disconnect.connect(self._worker.do_disconnect)
        self.sig_read.connect(self._worker.do_read)
        self.sig_write.connect(self._worker.do_write)
        self.sig_start_poll.connect(self._worker.start_poll)
        self.sig_stop_poll.connect(self._worker.stop_poll)
        self.sig_scan_slaves.connect(self._worker.scan_slaves)
        self.sig_scan_registers.connect(self._worker.scan_registers)
        self.sig_extract.connect(self._worker.do_extract)
        self.sig_validate.connect(self._worker.do_validate)

        # worker -> GUI
        self._worker.connected.connect(self._on_connected)
        self._worker.connection_failed.connect(self._on_connection_failed)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.read_done.connect(self._on_read_done)
        self._worker.write_done.connect(self._on_write_done)
        self._worker.poll_started.connect(lambda: self._set_polling(True))
        self._worker.poll_stopped.connect(lambda: self._set_polling(False))
        self._worker.log.connect(self._log)

        # discovery
        self._worker.slave_found.connect(self._on_slave_found)
        self._worker.slave_scan_progress.connect(self._on_slave_progress)
        self._worker.slave_scan_done.connect(self._on_slave_scan_done)
        self._worker.region_found.connect(self._on_region_found)
        self._worker.register_scan_progress.connect(self._on_register_progress)
        self._worker.register_scan_done.connect(self._on_register_scan_done)

        # register map
        self._worker.extract_done.connect(self._on_extract_done)
        self._worker.extract_failed.connect(self._on_extract_failed)
        self._worker.validate_progress.connect(self._on_validate_progress)
        self._worker.validate_done.connect(self._on_validate_done)

        self._thread.start()

    # ================================================================= #
    # UI construction
    # ================================================================= #
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # ---- left column ------------------------------------------- #
        left = QVBoxLayout()
        left.addWidget(self._build_connection_group())
        left.addWidget(self._build_netinfo_group())
        left.addStretch(1)
        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setFixedWidth(340)
        root.addWidget(left_wrap)

        # ---- right column: tabs + shared log ----------------------- #
        right = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_manual_tab(), "Manual")
        self.tabs.addTab(self._build_discovery_tab(), "Discovery")
        self.tabs.addTab(self._build_registermap_tab(), "Register Map (AI)")
        right.addWidget(self.tabs, stretch=1)
        right.addWidget(self._build_log_group())
        root.addLayout(right, stretch=1)

    def _build_manual_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._build_query_group())
        v.addWidget(self._build_results_group(), stretch=1)
        return w

    # -- connection ------------------------------------------------- #
    def _build_connection_group(self) -> QGroupBox:
        box = QGroupBox("Connection")
        v = QVBoxLayout(box)

        # transport selector
        sel = QHBoxLayout()
        self.rb_tcp = QRadioButton("TCP / IP")
        self.rb_serial = QRadioButton("Serial (RTU)")
        self.rb_tcp.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_tcp)
        grp.addButton(self.rb_serial)
        sel.addWidget(self.rb_tcp)
        sel.addWidget(self.rb_serial)
        sel.addStretch(1)
        v.addLayout(sel)

        # stacked settings
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_tcp_page())
        self.stack.addWidget(self._build_serial_page())
        v.addWidget(self.stack)

        self.rb_tcp.toggled.connect(
            lambda on: self.stack.setCurrentIndex(0) if on else None)
        self.rb_serial.toggled.connect(
            lambda on: self.stack.setCurrentIndex(1) if on else None)

        # connect button + status
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._toggle_connect)
        v.addWidget(self.btn_connect)

        self.lbl_status = QLabel("Disconnected")
        self.lbl_status.setStyleSheet("color: #b00; font-weight: bold;")
        v.addWidget(self.lbl_status)
        return box

    def _build_tcp_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.ed_host = QLineEdit("192.168.1.10")
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(502)
        self.sp_tcp_timeout = QDoubleSpinBox()
        self.sp_tcp_timeout.setRange(0.1, 30.0)
        self.sp_tcp_timeout.setValue(2.0)
        self.sp_tcp_timeout.setSuffix(" s")
        f.addRow("Slave IP:", self.ed_host)
        f.addRow("TCP port:", self.sp_port)
        f.addRow("Timeout:", self.sp_tcp_timeout)
        return w

    def _build_serial_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.cb_serial_port = QComboBox()
        self.cb_serial_port.setEditable(True)
        btn_refresh = QPushButton("Refresh ports")
        btn_refresh.clicked.connect(self._refresh_serial_ports)

        self.cb_baud = QComboBox()
        for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
            self.cb_baud.addItem(str(b), b)
        self.cb_baud.setCurrentText("19200")

        self.cb_databits = QComboBox()
        for d in (7, 8):
            self.cb_databits.addItem(str(d), d)
        self.cb_databits.setCurrentText("8")

        self.cb_parity = QComboBox()
        for label, val in (("None", "N"), ("Even", "E"), ("Odd", "O")):
            self.cb_parity.addItem(label, val)

        self.cb_stopbits = QComboBox()
        for s in (1, 2):
            self.cb_stopbits.addItem(str(s), s)

        self.sp_ser_timeout = QDoubleSpinBox()
        self.sp_ser_timeout.setRange(0.1, 30.0)
        self.sp_ser_timeout.setValue(2.0)
        self.sp_ser_timeout.setSuffix(" s")

        f.addRow("COM port:", self.cb_serial_port)
        f.addRow("", btn_refresh)
        f.addRow("Baud rate:", self.cb_baud)
        f.addRow("Data bits:", self.cb_databits)
        f.addRow("Parity:", self.cb_parity)
        f.addRow("Stop bits:", self.cb_stopbits)
        f.addRow("Timeout:", self.sp_ser_timeout)
        return w

    # -- PC network info ------------------------------------------- #
    def _build_netinfo_group(self) -> QGroupBox:
        box = QGroupBox("PC Network Info (read-only)")
        v = QVBoxLayout(box)
        self.txt_netinfo = QPlainTextEdit()
        self.txt_netinfo.setReadOnly(True)
        self.txt_netinfo.setFont(QFont("Consolas", 9))
        self.txt_netinfo.setFixedHeight(150)
        v.addWidget(self.txt_netinfo)
        btn = QPushButton("Refresh")
        btn.clicked.connect(self._refresh_net_info)
        v.addWidget(btn)
        return box

    # -- query builder --------------------------------------------- #
    def _build_query_group(self) -> QGroupBox:
        box = QGroupBox("Query")
        g = QGridLayout(box)

        # slave id
        g.addWidget(QLabel("Slave / Unit ID:"), 0, 0)
        self.sp_unit = QSpinBox()
        self.sp_unit.setRange(0, 255)
        self.sp_unit.setValue(1)
        g.addWidget(self.sp_unit, 0, 1)

        # function
        g.addWidget(QLabel("Function:"), 0, 2)
        self.cb_function = QComboBox()
        for fn in ALL_FUNCTIONS:
            self.cb_function.addItem(fn.label, fn)
        self.cb_function.currentIndexChanged.connect(self._on_function_changed)
        g.addWidget(self.cb_function, 0, 3, 1, 3)

        # address + base
        g.addWidget(QLabel("Start address:"), 1, 0)
        self.sp_address = QSpinBox()
        self.sp_address.setRange(0, 65535)
        self.sp_address.setValue(0)
        g.addWidget(self.sp_address, 1, 1)

        g.addWidget(QLabel("Address base:"), 1, 2)
        self.cb_base = QComboBox()
        self.cb_base.addItem("0-based (protocol)", 0)
        self.cb_base.addItem("1-based (40001-style)", 1)
        g.addWidget(self.cb_base, 1, 3)

        # count
        g.addWidget(QLabel("Quantity:"), 1, 4)
        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, MAX_REGISTERS)
        self.sp_count.setValue(10)
        g.addWidget(self.sp_count, 1, 5)

        # display format + word order
        g.addWidget(QLabel("Display format:"), 2, 0)
        self.cb_format = QComboBox()
        for fmt in DisplayFormat:
            self.cb_format.addItem(fmt.value, fmt)
        self.cb_format.currentIndexChanged.connect(self._refresh_last_decode)
        g.addWidget(self.cb_format, 2, 1, 1, 2)

        g.addWidget(QLabel("Word order:"), 2, 3)
        self.cb_word_order = QComboBox()
        for label, code in WORD_ORDERS:
            self.cb_word_order.addItem(label, code)
        self.cb_word_order.currentIndexChanged.connect(self._refresh_last_decode)
        g.addWidget(self.cb_word_order, 2, 4, 1, 2)

        # write values
        g.addWidget(QLabel("Write value(s):"), 3, 0)
        self.ed_write = QLineEdit()
        self.ed_write.setPlaceholderText("comma-separated, e.g. 100, 200  (coils: 0/1)")
        g.addWidget(self.ed_write, 3, 1, 1, 5)

        # action buttons
        actions = QHBoxLayout()
        self.btn_read = QPushButton("Read Once")
        self.btn_read.clicked.connect(self._do_read_once)
        actions.addWidget(self.btn_read)

        self.btn_write = QPushButton("Write")
        self.btn_write.clicked.connect(self._do_write)
        actions.addWidget(self.btn_write)

        actions.addSpacing(20)
        actions.addWidget(QLabel("Poll every:"))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(50, 60000)
        self.sp_interval.setValue(1000)
        self.sp_interval.setSuffix(" ms")
        actions.addWidget(self.sp_interval)

        self.btn_poll = QPushButton("Start Poll")
        self.btn_poll.clicked.connect(self._toggle_poll)
        actions.addWidget(self.btn_poll)
        actions.addStretch(1)
        g.addLayout(actions, 4, 0, 1, 6)

        return box

    # -- results table --------------------------------------------- #
    def _build_results_group(self) -> QGroupBox:
        box = QGroupBox("Results")
        v = QVBoxLayout(box)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Address", "Raw (hex)", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table)
        return box

    # -- log -------------------------------------------------------- #
    def _build_log_group(self) -> QGroupBox:
        box = QGroupBox("Activity log")
        v = QVBoxLayout(box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFixedHeight(120)
        self.txt_log.setFont(QFont("Consolas", 9))
        v.addWidget(self.txt_log)
        return box

    # ================================================================= #
    # Discovery tab
    # ================================================================= #
    def _build_discovery_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._build_slave_scan_group())
        v.addWidget(self._build_register_scan_group(), stretch=1)
        v.addWidget(self._build_suggestions_group(), stretch=1)
        return w

    def _build_slave_scan_group(self) -> QGroupBox:
        box = QGroupBox("1 - Slave discovery  (which Unit IDs respond)")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Unit ID range:"))
        self.sp_scan_unit_start = QSpinBox()
        self.sp_scan_unit_start.setRange(0, 255)
        self.sp_scan_unit_start.setValue(1)
        row.addWidget(self.sp_scan_unit_start)
        row.addWidget(QLabel("to"))
        self.sp_scan_unit_end = QSpinBox()
        self.sp_scan_unit_end.setRange(0, 255)
        self.sp_scan_unit_end.setValue(32)
        row.addWidget(self.sp_scan_unit_end)

        row.addSpacing(12)
        row.addWidget(QLabel("Probe:"))
        self.cb_scan_probe_func = QComboBox()
        for fn in READ_FUNCTIONS:
            self.cb_scan_probe_func.addItem(fn.label, fn)
        row.addWidget(self.cb_scan_probe_func)
        row.addWidget(QLabel("@ addr"))
        self.sp_scan_probe_addr = QSpinBox()
        self.sp_scan_probe_addr.setRange(0, 65535)
        self.sp_scan_probe_addr.setValue(0)
        row.addWidget(self.sp_scan_probe_addr)

        self.btn_scan_slaves = QPushButton("Scan Slaves")
        self.btn_scan_slaves.clicked.connect(self._do_scan_slaves)
        row.addWidget(self.btn_scan_slaves)
        row.addStretch(1)
        v.addLayout(row)

        prow = QHBoxLayout()
        self.pb_slave = QProgressBar()
        self.pb_slave.setValue(0)
        prow.addWidget(self.pb_slave, stretch=1)
        self.btn_cancel_slave = QPushButton("Cancel")
        self.btn_cancel_slave.clicked.connect(self._cancel_scan)
        self.btn_cancel_slave.setEnabled(False)
        prow.addWidget(self.btn_cancel_slave)
        v.addLayout(prow)

        self.tbl_slaves = QTableWidget(0, 3)
        self.tbl_slaves.setHorizontalHeaderLabels(["Unit ID", "Status", "Detail"])
        self.tbl_slaves.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl_slaves.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tbl_slaves.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_slaves.verticalHeader().setVisible(False)
        self.tbl_slaves.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_slaves.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_slaves.setMaximumHeight(150)
        self.tbl_slaves.itemSelectionChanged.connect(self._on_slave_selected)
        v.addWidget(self.tbl_slaves)
        v.addWidget(QLabel("Tip: select a responding slave to load it into the register scan below."))
        return box

    def _build_register_scan_group(self) -> QGroupBox:
        box = QGroupBox("2 - Register discovery  (which addresses return data)")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Unit:"))
        self.sp_rscan_unit = QSpinBox()
        self.sp_rscan_unit.setRange(0, 255)
        self.sp_rscan_unit.setValue(1)
        row.addWidget(self.sp_rscan_unit)

        row.addWidget(QLabel("Function:"))
        self.cb_rscan_func = QComboBox()
        for fn in READ_FUNCTIONS:
            self.cb_rscan_func.addItem(fn.label, fn)
        row.addWidget(self.cb_rscan_func)

        row.addWidget(QLabel("Addr:"))
        self.sp_rscan_start = QSpinBox()
        self.sp_rscan_start.setRange(0, 65535)
        self.sp_rscan_start.setValue(0)
        row.addWidget(self.sp_rscan_start)
        row.addWidget(QLabel("to"))
        self.sp_rscan_end = QSpinBox()
        self.sp_rscan_end.setRange(0, 65535)
        self.sp_rscan_end.setValue(120)
        row.addWidget(self.sp_rscan_end)

        row.addWidget(QLabel("Block:"))
        self.sp_rscan_block = QSpinBox()
        self.sp_rscan_block.setRange(1, MAX_REGISTERS)
        self.sp_rscan_block.setValue(10)
        row.addWidget(self.sp_rscan_block)

        self.btn_scan_regs = QPushButton("Scan Registers")
        self.btn_scan_regs.clicked.connect(self._do_scan_registers)
        row.addWidget(self.btn_scan_regs)
        row.addStretch(1)
        v.addLayout(row)

        prow = QHBoxLayout()
        self.pb_regs = QProgressBar()
        prow.addWidget(self.pb_regs, stretch=1)
        self.btn_cancel_regs = QPushButton("Cancel")
        self.btn_cancel_regs.clicked.connect(self._cancel_scan)
        self.btn_cancel_regs.setEnabled(False)
        prow.addWidget(self.btn_cancel_regs)
        v.addLayout(prow)

        self.tbl_regions = QTableWidget(0, 5)
        self.tbl_regions.setHorizontalHeaderLabels(
            ["Address", "Count", "Status", "Values (preview)", "Suggested format"])
        hh = self.tbl_regions.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_regions.verticalHeader().setVisible(False)
        self.tbl_regions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_regions.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_regions.itemSelectionChanged.connect(self._on_region_selected)
        v.addWidget(self.tbl_regions)
        return box

    def _build_suggestions_group(self) -> QGroupBox:
        box = QGroupBox("3 - Format suggestions  (for the selected responding block)")
        v = QVBoxLayout(box)
        self.tbl_suggest = QTableWidget(0, 5)
        self.tbl_suggest.setHorizontalHeaderLabels(
            ["Format", "Byte order", "Confidence", "Sample", "Why"])
        hh = self.tbl_suggest.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_suggest.verticalHeader().setVisible(False)
        self.tbl_suggest.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_suggest.setSelectionBehavior(QTableWidget.SelectRows)
        v.addWidget(self.tbl_suggest)

        brow = QHBoxLayout()
        brow.addStretch(1)
        self.btn_apply_suggest = QPushButton("Apply selected format to Manual tab")
        self.btn_apply_suggest.clicked.connect(self._apply_suggestion)
        self.btn_apply_suggest.setEnabled(False)
        brow.addWidget(self.btn_apply_suggest)
        v.addLayout(brow)
        return box

    # ================================================================= #
    # Register Map (AI) tab
    # ================================================================= #
    _RM_COLS = ["Register", "Table", "Base", "Addr", "Name", "Type", "Word",
                "Scale", "Offset", "Unit", "Access", "Status", "Live value", "Pg"]
    _RM_EDITABLE = {0, 1, 2, 4, 5, 6, 7, 8, 9, 10}
    _RM_STATUS_COLORS = {
        "verified": QColor(200, 245, 200),
        "mismatch": QColor(255, 225, 190),
        "unread": QColor(235, 235, 235),
        "unverified": QColor(255, 255, 255),
    }

    def _build_registermap_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._build_extract_group())
        v.addWidget(self._build_profiles_group(), stretch=1)
        return w

    def _build_extract_group(self) -> QGroupBox:
        box = QGroupBox("1 - Extract register map from a vendor PDF")
        v = QVBoxLayout(box)

        # PDF picker
        prow = QHBoxLayout()
        prow.addWidget(QLabel("PDF:"))
        self.ed_pdf = QLineEdit()
        self.ed_pdf.setPlaceholderText("path to the device's Modbus register PDF")
        prow.addWidget(self.ed_pdf, stretch=1)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._rm_browse_pdf)
        prow.addWidget(btn_browse)
        prow.addWidget(QLabel("Pages:"))
        self.ed_pages = QLineEdit()
        self.ed_pages.setPlaceholderText("all")
        self.ed_pages.setFixedWidth(90)
        prow.addWidget(self.ed_pages)
        v.addLayout(prow)

        # provider
        sel = QHBoxLayout()
        self.rb_anthropic = QRadioButton("Anthropic (cloud)")
        self.rb_lmstudio = QRadioButton("LM Studio (local)")
        self.rb_anthropic.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_anthropic)
        grp.addButton(self.rb_lmstudio)
        sel.addWidget(self.rb_anthropic)
        sel.addWidget(self.rb_lmstudio)
        sel.addStretch(1)
        v.addLayout(sel)

        self.rm_stack = QStackedWidget()
        self.rm_stack.addWidget(self._build_anthropic_page())
        self.rm_stack.addWidget(self._build_lmstudio_page())
        v.addWidget(self.rm_stack)
        self.rb_anthropic.toggled.connect(
            lambda on: self.rm_stack.setCurrentIndex(0) if on else None)
        self.rb_lmstudio.toggled.connect(
            lambda on: self.rm_stack.setCurrentIndex(1) if on else None)

        # actions
        arow = QHBoxLayout()
        self.btn_test_provider = QPushButton("Test provider")
        self.btn_test_provider.clicked.connect(self._do_test_provider)
        arow.addWidget(self.btn_test_provider)
        self.btn_extract = QPushButton("Extract register map")
        self.btn_extract.clicked.connect(self._do_extract)
        arow.addWidget(self.btn_extract)
        arow.addStretch(1)
        v.addLayout(arow)
        return box

    def _build_anthropic_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.ed_anthropic_model = QLineEdit(DEFAULT_ANTHROPIC_MODEL)
        f.addRow("Model:", self.ed_anthropic_model)
        note = QLabel("Reads ANTHROPIC_API_KEY from your environment. "
                      "The PDF is sent to Anthropic's API.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        f.addRow("", note)
        return w

    def _build_lmstudio_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.ed_lm_url = QLineEdit("http://localhost:1234/v1")
        self.ed_lm_model = QLineEdit()
        self.ed_lm_model.setPlaceholderText("model name loaded in LM Studio")
        f.addRow("Base URL:", self.ed_lm_url)
        f.addRow("Model:", self.ed_lm_model)
        note = QLabel("Fully local - the PDF text never leaves your machine. "
                      "(Works with Ollama's OpenAI endpoint too.)")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        f.addRow("", note)
        return w

    def _build_profiles_group(self) -> QGroupBox:
        box = QGroupBox("2 - Review, validate against the device, and save")
        v = QVBoxLayout(box)

        self.tbl_profiles = QTableWidget(0, len(self._RM_COLS))
        self.tbl_profiles.setHorizontalHeaderLabels(self._RM_COLS)
        self.tbl_profiles.verticalHeader().setVisible(False)
        self.tbl_profiles.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_profiles.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_profiles.itemChanged.connect(self._on_profile_item_changed)
        v.addWidget(self.tbl_profiles)

        prow = QHBoxLayout()
        self.pb_ai = QProgressBar()
        prow.addWidget(self.pb_ai, stretch=1)
        v.addLayout(prow)

        brow = QHBoxLayout()
        self.btn_validate = QPushButton("Validate against device")
        self.btn_validate.clicked.connect(self._do_validate)
        brow.addWidget(self.btn_validate)
        self.btn_send_manual = QPushButton("Send selected to Manual")
        self.btn_send_manual.clicked.connect(self._rm_send_to_manual)
        brow.addWidget(self.btn_send_manual)
        brow.addStretch(1)
        self.btn_load_profile = QPushButton("Load profile...")
        self.btn_load_profile.clicked.connect(self._do_load_profile)
        brow.addWidget(self.btn_load_profile)
        self.btn_save_profile = QPushButton("Save profile...")
        self.btn_save_profile.clicked.connect(self._do_save_profile)
        brow.addWidget(self.btn_save_profile)
        v.addLayout(brow)

        legend = QLabel("Status colors: green = verified against device, "
                        "amber = read OK but value looks implausible (check "
                        "type/word order/scale), grey = no response (address may "
                        "be wrong). Editable cells re-normalize automatically.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #666;")
        v.addWidget(legend)
        return box

    # ---- provider construction ------------------------------------ #
    def _rm_browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Modbus register PDF", "", "PDF files (*.pdf);;All files (*)")
        if path:
            self.ed_pdf.setText(path)

    def _build_provider(self):
        if self.rb_anthropic.isChecked():
            model = self.ed_anthropic_model.text().strip() or DEFAULT_ANTHROPIC_MODEL
            return AnthropicProvider(model=model)
        url = self.ed_lm_url.text().strip()
        model = self.ed_lm_model.text().strip()
        if not model:
            QMessageBox.information(self, "Model", "Enter the LM Studio model name.")
            return None
        return OpenAICompatProvider(base_url=url, model=model)

    def _do_test_provider(self):
        provider = self._build_provider()
        if provider is None:
            return
        ok, detail = provider.check()
        if ok:
            self._flash_status(f"Provider OK: {detail}", error=False)
            self._log(f"Provider check OK: {detail}")
        else:
            QMessageBox.warning(self, "Provider check failed", detail)

    # ---- extraction ----------------------------------------------- #
    def _do_extract(self):
        import os
        provider = self._build_provider()
        if provider is None:
            return
        pdf = self.ed_pdf.text().strip()
        if not pdf or not os.path.isfile(pdf):
            QMessageBox.information(self, "PDF", "Choose an existing PDF file.")
            return
        self._set_ai_busy(True)
        self._flash_status("Extracting register map ...", error=False)
        self.sig_extract.emit(ExtractRequest(
            provider=provider, pdf_path=pdf, page_range=self.ed_pages.text().strip()))

    @Slot(list)
    def _on_extract_done(self, profiles):
        self._set_ai_busy(False)
        self._profiles = profiles
        self._render_profiles()
        self._flash_status(f"Extracted {len(profiles)} register(s)", error=False)

    @Slot(str)
    def _on_extract_failed(self, err):
        self._set_ai_busy(False)
        QMessageBox.warning(self, "Extraction failed", err)
        self._flash_status("Extraction failed", error=True)

    def _render_profiles(self):
        self.tbl_profiles.blockSignals(True)
        self.tbl_profiles.setRowCount(0)
        for p in self._profiles:
            r = self.tbl_profiles.rowCount()
            self.tbl_profiles.insertRow(r)
            addr = "" if p.protocol_address is None else str(p.protocol_address)
            enums = "" if not p.enums else " ; ".join(
                f"{k}={v}" for k, v in list(p.enums.items())[:4])
            live = p.live_value + ("  " + enums if enums else "")
            cells = [
                p.printed_register, p.table, p.address_base, addr, p.name,
                p.data_type, p.word_order, f"{p.scale:g}", f"{p.offset:g}",
                p.unit, p.access, p.status, live, str(p.source_page or ""),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c not in self._RM_EDITABLE:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 11:  # status color across the row's status cell
                    item.setBackground(self._RM_STATUS_COLORS.get(p.status, QColor(255, 255, 255)))
                self.tbl_profiles.setItem(r, c, item)
        self.tbl_profiles.blockSignals(False)

    def _on_profile_item_changed(self, item):
        """User edited a cell - push it back into the profile and re-normalize."""
        r, c = item.row(), item.column()
        if r >= len(self._profiles) or c not in self._RM_EDITABLE:
            return
        p = self._profiles[r]
        text = item.text().strip()
        if c == 0:
            p.printed_register = text
        elif c == 1:
            p.table = text.lower()
        elif c == 2:
            p.address_base = text.lower()
        elif c == 4:
            p.name = text
        elif c == 5:
            p.data_type = text
        elif c == 6:
            p.word_order = text.upper() or "ABCD"
        elif c == 7:
            try:
                p.scale = float(text)
            except ValueError:
                pass
        elif c == 8:
            try:
                p.offset = float(text)
            except ValueError:
                pass
        elif c == 9:
            p.unit = text
        elif c == 10:
            p.access = text.upper()
        p.normalize()
        # refresh the computed Addr cell without recursing
        self.tbl_profiles.blockSignals(True)
        addr = "" if p.protocol_address is None else str(p.protocol_address)
        self.tbl_profiles.item(r, 3).setText(addr)
        self.tbl_profiles.blockSignals(False)

    # ---- validation ----------------------------------------------- #
    def _do_validate(self):
        if not self._profiles:
            QMessageBox.information(self, "No map", "Extract or load a register map first.")
            return
        if not self._connected:
            QMessageBox.information(self, "Not connected", "Connect to the device first.")
            return
        self._set_ai_busy(True)
        self.pb_ai.setMaximum(len(self._profiles))
        self.pb_ai.setValue(0)
        self.sig_validate.emit(ValidateRequest(
            profiles=self._profiles, unit=self.sp_unit.value()))

    @Slot(int, int)
    def _on_validate_progress(self, done, total):
        self.pb_ai.setMaximum(total)
        self.pb_ai.setValue(done)

    @Slot(object)
    def _on_validate_done(self, summary):
        self._set_ai_busy(False)
        self._render_profiles()
        self._flash_status(
            f"Validated: {summary.get('verified', 0)} verified, "
            f"{summary.get('mismatch', 0)} mismatch, "
            f"{summary.get('unread', 0)} unread", error=False)

    # ---- save / load / send --------------------------------------- #
    def _do_save_profile(self):
        if not self._profiles:
            QMessageBox.information(self, "No map", "Nothing to save.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save device profile", "device_profile.json", "JSON (*.json)")
        if not path:
            return
        try:
            profile_store.save(path, self._profiles)
            self._flash_status(f"Saved {len(self._profiles)} register(s)", error=False)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(exc))

    def _do_load_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load device profile", "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            _name, profiles = profile_store.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        self._profiles = profiles
        self._render_profiles()
        self._flash_status(f"Loaded {len(profiles)} register(s)", error=False)

    def _rm_send_to_manual(self):
        rows = self.tbl_profiles.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._profiles):
            return
        p = self._profiles[rows[0].row()]
        if p.protocol_address is None:
            QMessageBox.information(self, "No address", p.norm_note or "Cannot resolve address.")
            return
        from .ai.schema import read_function_for
        self._select_function(read_function_for(p.table))
        self.cb_base.setCurrentIndex(0)
        self.sp_address.setValue(p.protocol_address)
        if p.resolved_format is not None:
            self._select_data(self.cb_format, p.resolved_format)
            self._select_data(self.cb_word_order, p.word_order)
        self.tabs.setCurrentIndex(0)
        self._flash_status(f"Loaded '{p.name or p.printed_register}' into Manual tab",
                           error=False)

    def _set_ai_busy(self, on: bool):
        self._busy_ai = on
        for w in (self.btn_extract, self.btn_test_provider, self.btn_validate,
                  self.btn_load_profile, self.btn_save_profile):
            w.setEnabled(not on)
        if not on:
            # validate needs a connection
            self.btn_validate.setEnabled(True)

    # ================================================================= #
    # Helpers to read the UI
    # ================================================================= #
    def _current_function(self):
        return self.cb_function.currentData()

    def _protocol_address(self) -> int:
        """Convert the user-entered address into a 0-based protocol address."""
        addr = self.sp_address.value()
        if self.cb_base.currentData() == 1:  # 1-based
            addr = max(0, addr - 1)
        return addr

    def _refresh_serial_ports(self):
        current = self.cb_serial_port.currentText()
        self.cb_serial_port.clear()
        ports = list(list_ports.comports())
        for p in ports:
            self.cb_serial_port.addItem(p.device, p.device)
        if current:
            self.cb_serial_port.setCurrentText(current)
        self._log(f"Found {len(ports)} serial port(s)")

    def _refresh_net_info(self):
        try:
            self.txt_netinfo.setPlainText(net_info.summary_text())
        except Exception as exc:  # noqa: BLE001
            self.txt_netinfo.setPlainText(f"Could not read network info: {exc}")

    # ================================================================= #
    # Connection
    # ================================================================= #
    def _toggle_connect(self):
        if self._connected:
            self.sig_disconnect.emit()
        else:
            self.sig_connect.emit(self._build_config())

    def _build_config(self):
        if self.rb_tcp.isChecked():
            return TcpConfig(
                host=self.ed_host.text().strip(),
                port=self.sp_port.value(),
                timeout=self.sp_tcp_timeout.value(),
            )
        return SerialConfig(
            port=self.cb_serial_port.currentText().strip(),
            baudrate=self.cb_baud.currentData(),
            bytesize=self.cb_databits.currentData(),
            parity=self.cb_parity.currentData(),
            stopbits=self.cb_stopbits.currentData(),
            timeout=self.sp_ser_timeout.value(),
        )

    @Slot(str)
    def _on_connected(self, desc: str):
        self._connected = True
        self.lbl_status.setText(f"Connected  ({desc})")
        self.lbl_status.setStyleSheet("color: #080; font-weight: bold;")
        self._update_connection_state(True)

    @Slot(str)
    def _on_connection_failed(self, err: str):
        self._connected = False
        self.lbl_status.setText("Connect failed")
        self.lbl_status.setStyleSheet("color: #b00; font-weight: bold;")
        QMessageBox.warning(self, "Connection failed", err)

    @Slot()
    def _on_disconnected(self):
        self._connected = False
        self._set_polling(False)
        self.lbl_status.setText("Disconnected")
        self.lbl_status.setStyleSheet("color: #b00; font-weight: bold;")
        self._update_connection_state(False)

    def _update_connection_state(self, connected: bool):
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        # lock transport settings while connected
        self.rb_tcp.setEnabled(not connected)
        self.rb_serial.setEnabled(not connected)
        self.stack.setEnabled(not connected)
        # enable actions only when connected
        self.btn_read.setEnabled(connected)
        self.btn_poll.setEnabled(connected)
        self.btn_scan_slaves.setEnabled(connected)
        self.btn_scan_regs.setEnabled(connected)
        self._sync_write_enabled()

    # ================================================================= #
    # Query / function changes
    # ================================================================= #
    def _on_function_changed(self):
        fn = self._current_function()
        is_write = fn.access == "write"
        is_bits = fn.kind is DataKind.BITS

        # format / word order only meaningful for register reads
        fmt_enabled = (not is_write) and (not is_bits)
        self.cb_format.setEnabled(fmt_enabled)
        self.cb_word_order.setEnabled(fmt_enabled and is_wide(self.cb_format.currentData()))

        # quantity only meaningful for reads and write-multiple
        self.sp_count.setEnabled(not is_write or fn.multi)
        self._sync_write_enabled()

    def _sync_write_enabled(self):
        fn = self._current_function()
        is_write = fn.access == "write"
        self.ed_write.setEnabled(self._connected and is_write)
        self.btn_write.setEnabled(self._connected and is_write)

    # ================================================================= #
    # Read
    # ================================================================= #
    def _do_read_once(self):
        fn = self._current_function()
        if fn.access != "read":
            QMessageBox.information(self, "Not a read", "Select a read function first.")
            return
        req = ReadRequest(
            func=fn,
            address=self._protocol_address(),
            count=self.sp_count.value(),
            unit=self.sp_unit.value(),
        )
        self.sig_read.emit(req)

    @Slot(object, object, int)
    def _on_read_done(self, result, req, seq):
        if not result.ok:
            self._flash_status(result.error, error=True)
            return
        # remember for re-decode when the user changes format
        self._last_read = (result, req)
        self._render_result(result, req)

    def _refresh_last_decode(self):
        # keep word-order enable state in sync with the chosen format
        fn = self._current_function()
        if fn.access == "read" and fn.kind is DataKind.REGISTERS:
            self.cb_word_order.setEnabled(is_wide(self.cb_format.currentData()))
        last = getattr(self, "_last_read", None)
        if last is not None:
            self._render_result(*last)

    def _render_result(self, result, req):
        if req.func.kind is DataKind.BITS:
            rows = decode_bits(result.values, req.address)
        else:
            rows = decode_registers(
                result.values, req.address,
                self.cb_format.currentData(),
                self.cb_word_order.currentData(),
            )
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(str(row.address)))
            self.table.setItem(r, 1, QTableWidgetItem(row.raw_text))
            self.table.setItem(r, 2, QTableWidgetItem(row.value_text))

    # ================================================================= #
    # Write
    # ================================================================= #
    def _do_write(self):
        fn = self._current_function()
        if fn.access != "write":
            return
        values = self._parse_write_values(fn.kind)
        if values is None:
            return
        if not fn.multi:
            values = values[:1]
        req = WriteRequest(
            func=fn,
            address=self._protocol_address(),
            values=values,
            unit=self.sp_unit.value(),
        )
        self.sig_write.emit(req)

    def _parse_write_values(self, kind: DataKind):
        text = self.ed_write.text().strip()
        if not text:
            QMessageBox.information(self, "No value", "Enter value(s) to write.")
            return None
        parts = [p for p in text.replace(",", " ").split() if p]
        out = []
        try:
            for p in parts:
                if kind is DataKind.BITS:
                    low = p.lower()
                    if low in ("1", "on", "true"):
                        out.append(1)
                    elif low in ("0", "off", "false"):
                        out.append(0)
                    else:
                        raise ValueError(p)
                else:
                    val = int(p, 0)  # supports 0x.. hex and decimal
                    if not 0 <= val <= 0xFFFF:
                        raise ValueError(f"{val} out of 16-bit range")
                    out.append(val)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", f"Could not parse '{exc}'.")
            return None
        return out

    @Slot(object, object)
    def _on_write_done(self, result, req):
        if result.ok:
            self._flash_status(f"Wrote {req.values} to {req.address}", error=False)
        else:
            self._flash_status(result.error, error=True)

    # ================================================================= #
    # Poll
    # ================================================================= #
    def _toggle_poll(self):
        if self._polling:
            self.sig_stop_poll.emit()
            return
        fn = self._current_function()
        if fn.access != "read":
            QMessageBox.information(self, "Not a read", "Polling requires a read function.")
            return
        read_req = ReadRequest(
            func=fn,
            address=self._protocol_address(),
            count=self.sp_count.value(),
            unit=self.sp_unit.value(),
        )
        self.sig_start_poll.emit(PollRequest(read=read_req, interval_ms=self.sp_interval.value()))

    def _set_polling(self, on: bool):
        self._polling = on
        self.btn_poll.setText("Stop Poll" if on else "Start Poll")
        # while polling, lock the query fields that define the poll
        for w in (self.cb_function, self.sp_address, self.sp_count,
                  self.sp_unit, self.cb_base, self.btn_read):
            w.setEnabled(not on)
        if not on and self._connected:
            self.btn_read.setEnabled(True)

    # ================================================================= #
    # Misc
    # ================================================================= #
    # ================================================================= #
    # Discovery: slave scan
    # ================================================================= #
    _COLORS = {
        "responding": QColor(200, 245, 200),
        "ok": QColor(200, 245, 200),
        "exception": QColor(255, 235, 190),
        "no-response": QColor(240, 240, 240),
    }

    def _do_scan_slaves(self):
        if self.sp_scan_unit_end.value() < self.sp_scan_unit_start.value():
            QMessageBox.information(self, "Range", "End Unit ID must be >= start.")
            return
        self.tbl_slaves.setRowCount(0)
        self._slave_rows.clear()
        self.pb_slave.setValue(0)
        self._set_scanning(True)
        self.sig_scan_slaves.emit(SlaveScanRequest(
            start_unit=self.sp_scan_unit_start.value(),
            end_unit=self.sp_scan_unit_end.value(),
            func=self.cb_scan_probe_func.currentData(),
            address=self.sp_scan_probe_addr.value(),
        ))

    @Slot(object)
    def _on_slave_found(self, res):
        self._slave_rows.append(res)
        r = self.tbl_slaves.rowCount()
        self.tbl_slaves.insertRow(r)
        cells = [str(res.unit), res.status, res.detail]
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setBackground(self._COLORS.get(res.status, QColor(255, 255, 255)))
            self.tbl_slaves.setItem(r, c, item)

    @Slot(int, int)
    def _on_slave_progress(self, done, total):
        self.pb_slave.setMaximum(total)
        self.pb_slave.setValue(done)

    @Slot(object)
    def _on_slave_scan_done(self, summary):
        self._set_scanning(False)
        self._flash_status(
            f"Slave scan: {len(summary.responding)} responding, "
            f"{len(summary.exceptions)} answered with exception", error=False)

    def _on_slave_selected(self):
        rows = self.tbl_slaves.selectionModel().selectedRows()
        if not rows:
            return
        res = self._slave_rows[rows[0].row()]
        if res.status != "no-response":
            self.sp_rscan_unit.setValue(res.unit)

    # ================================================================= #
    # Discovery: register scan
    # ================================================================= #
    def _do_scan_registers(self):
        if self.sp_rscan_end.value() < self.sp_rscan_start.value():
            QMessageBox.information(self, "Range", "End address must be >= start.")
            return
        self.tbl_regions.setRowCount(0)
        self._region_rows.clear()
        self.tbl_suggest.setRowCount(0)
        self._current_suggestions.clear()
        self.btn_apply_suggest.setEnabled(False)
        self.pb_regs.setValue(0)
        self._set_scanning(True)
        # remember what was actually scanned for the suggestion/apply step
        self._rscan_func_used = self.cb_rscan_func.currentData()
        self._rscan_unit_used = self.sp_rscan_unit.value()
        self.sig_scan_registers.emit(RegisterScanRequest(
            unit=self._rscan_unit_used,
            func=self._rscan_func_used,
            start=self.sp_rscan_start.value(),
            end=self.sp_rscan_end.value(),
            block=self.sp_rscan_block.value(),
        ))

    @Slot(object)
    def _on_region_found(self, res):
        self._region_rows.append(res)
        r = self.tbl_regions.rowCount()
        self.tbl_regions.insertRow(r)
        preview = ""
        if res.values:
            preview = ", ".join(str(v) for v in res.values[:8])
            if len(res.values) > 8:
                preview += ", ..."
        cells = [str(res.address), str(res.count), res.status, preview, res.suggestion]
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setBackground(self._COLORS.get(res.status, QColor(255, 255, 255)))
            self.tbl_regions.setItem(r, c, item)

    @Slot(int, int)
    def _on_register_progress(self, done, total):
        self.pb_regs.setMaximum(total)
        self.pb_regs.setValue(done)

    @Slot(list)
    def _on_register_scan_done(self, regions):
        self._set_scanning(False)
        self._flash_status(
            f"Register scan: {len(regions)} responding block(s)", error=False)

    def _on_region_selected(self):
        rows = self.tbl_regions.selectionModel().selectedRows()
        if not rows:
            return
        res = self._region_rows[rows[0].row()]
        if res.status != "ok" or not res.values:
            self.tbl_suggest.setRowCount(0)
            self.btn_apply_suggest.setEnabled(False)
            return
        func = getattr(self, "_rscan_func_used", self.cb_rscan_func.currentData())
        unit = getattr(self, "_rscan_unit_used", self.sp_rscan_unit.value())
        self._suggest_ctx = (unit, func, res.address)
        self._current_suggestions = discovery.analyze_block(res.values)
        self._render_suggestions()

    def _render_suggestions(self):
        self.tbl_suggest.setRowCount(0)
        for s in self._current_suggestions:
            r = self.tbl_suggest.rowCount()
            self.tbl_suggest.insertRow(r)
            order = s.word_order or "-"
            conf = f"{s.confidence_label()} ({int(s.confidence * 100)}%)"
            for c, text in enumerate([s.fmt.value, order, conf, s.sample, s.reason]):
                self.tbl_suggest.setItem(r, c, QTableWidgetItem(text))
        self.btn_apply_suggest.setEnabled(bool(self._current_suggestions))
        if self._current_suggestions:
            self.tbl_suggest.selectRow(0)

    def _apply_suggestion(self):
        rows = self.tbl_suggest.selectionModel().selectedRows()
        if not rows or not self._current_suggestions:
            return
        s = self._current_suggestions[rows[0].row()]
        if self._suggest_ctx is None:
            return
        unit, func, address = self._suggest_ctx

        # push the whole context into the Manual tab so a Read reproduces it
        self._select_function(func)
        self.sp_unit.setValue(unit)
        self.cb_base.setCurrentIndex(0)  # protocol/0-based
        self.sp_address.setValue(address)
        self._select_data(self.cb_format, s.fmt)
        if s.word_order:
            self._select_data(self.cb_word_order, s.word_order)
        self.tabs.setCurrentIndex(0)
        self._flash_status(
            f"Applied {s.fmt.value} ({s.word_order or 'n/a'}) to Manual tab", error=False)

    @staticmethod
    def _select_data(combo: QComboBox, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _select_function(self, func):
        idx = self.cb_function.findData(func)
        if idx >= 0:
            self.cb_function.setCurrentIndex(idx)

    # ================================================================= #
    # Scan state / cancel
    # ================================================================= #
    def _cancel_scan(self):
        self._worker.request_cancel()
        self._log("Cancelling scan ...")

    def _set_scanning(self, on: bool):
        self._scanning = on
        for w in (self.btn_scan_slaves, self.btn_scan_regs):
            w.setEnabled(not on and self._connected)
        self.btn_cancel_slave.setEnabled(on)
        self.btn_cancel_regs.setEnabled(on)
        # lock connection changes during a scan
        self.btn_connect.setEnabled(not on)

    def _flash_status(self, msg: str, error: bool):
        self.lbl_status.setText(msg)
        color = "#b00" if error else "#080"
        self.lbl_status.setStyleSheet(f"color: {color}; font-weight: bold;")

    @Slot(str)
    def _log(self, msg: str):
        self.txt_log.appendPlainText(msg)

    def closeEvent(self, event):
        try:
            self.sig_stop_poll.emit()
            self.sig_disconnect.emit()
            self._thread.quit()
            self._thread.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)


def run():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("ModbusTool")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
