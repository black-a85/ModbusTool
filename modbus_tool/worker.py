"""Background Modbus worker.

All pymodbus calls block, so they run in a dedicated QThread. The GUI talks to
the worker exclusively through queued signals/slots, which keeps the UI
responsive and makes polling a simple QTimer living inside the worker thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Union

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .modbus_client import (
    ModbusService, ModbusFunction, TcpConfig, SerialConfig, OpResult,
)
from . import discovery
from .ai import extractor as ai_extractor
from .ai import validation as ai_validation


@dataclass
class ReadRequest:
    func: ModbusFunction
    address: int
    count: int
    unit: int


@dataclass
class WriteRequest:
    func: ModbusFunction
    address: int
    values: list
    unit: int


@dataclass
class PollRequest:
    read: ReadRequest
    interval_ms: int


@dataclass
class SlaveScanRequest:
    start_unit: int
    end_unit: int
    func: ModbusFunction
    address: int


@dataclass
class RegisterScanRequest:
    unit: int
    func: ModbusFunction
    start: int
    end: int
    block: int


@dataclass
class ExtractRequest:
    provider: object          # ai.providers.LLMProvider
    pdf_path: str
    page_range: str


@dataclass
class ValidateRequest:
    profiles: list            # list[ai.schema.RegisterProfile] (mutated in place)
    unit: int


class ModbusWorker(QObject):
    # outbound signals to the GUI
    connected = Signal(str)             # description
    connection_failed = Signal(str)     # error text
    disconnected = Signal()
    read_done = Signal(object, object, int)   # OpResult, ReadRequest, seq_no
    write_done = Signal(object, object)       # OpResult, WriteRequest
    poll_started = Signal()
    poll_stopped = Signal()
    log = Signal(str)

    # discovery signals
    slave_found = Signal(object)          # SlaveResult
    slave_scan_progress = Signal(int, int)  # done, total
    slave_scan_done = Signal(object)      # SlaveScanSummary
    region_found = Signal(object)         # RegionResult
    register_scan_progress = Signal(int, int)
    register_scan_done = Signal(list)     # list[RegionResult] (ok only)

    # AI register-map signals
    extract_done = Signal(list)           # list[RegisterProfile]
    extract_failed = Signal(str)
    validate_progress = Signal(int, int)
    validate_done = Signal(object)        # summary dict; profiles mutated in place

    def __init__(self):
        super().__init__()
        self._service = ModbusService()
        self._poll_timer: QTimer | None = None
        self._poll_req: ReadRequest | None = None
        self._seq = 0
        self._cancel = threading.Event()

    # cancellation is set directly from the GUI thread (thread-safe Event),
    # bypassing the queued event loop so it interrupts a running scan.
    def request_cancel(self) -> None:
        self._cancel.set()

    # --------------------------------------------------------------- #
    @Slot(object)
    def do_connect(self, cfg: Union[TcpConfig, SerialConfig]) -> None:
        try:
            if isinstance(cfg, TcpConfig):
                self._service.connect_tcp(cfg)
            else:
                self._service.connect_serial(cfg)
            self.log.emit(f"Connected: {self._service.description}")
            self.connected.emit(self._service.description)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"Connect failed: {exc}")
            self.connection_failed.emit(str(exc))

    @Slot()
    def do_disconnect(self) -> None:
        self._stop_poll_internal()
        self._service.close()
        self.log.emit("Disconnected")
        self.disconnected.emit()

    @Slot(object)
    def do_read(self, req: ReadRequest) -> None:
        self._seq += 1
        result = self._service.read(req.func, req.address, req.count, req.unit)
        self._log_result(req.func.label, req.address, result)
        self.read_done.emit(result, req, self._seq)

    @Slot(object)
    def do_write(self, req: WriteRequest) -> None:
        result = self._service.write(req.func, req.address, req.values, req.unit)
        if result.ok:
            self.log.emit(f"{req.func.label} @ {req.address}: wrote {req.values}")
        else:
            self.log.emit(f"{req.func.label} @ {req.address}: {result.error}")
        self.write_done.emit(result, req)

    # --------------------------------------------------------------- #
    @Slot(object)
    def start_poll(self, req: PollRequest) -> None:
        self._poll_req = req.read
        if self._poll_timer is None:
            self._poll_timer = QTimer()
            self._poll_timer.timeout.connect(self._on_poll_tick)
        self._poll_timer.setInterval(max(50, req.interval_ms))
        self._poll_timer.start()
        self.log.emit(f"Polling every {req.interval_ms} ms")
        self.poll_started.emit()
        self._on_poll_tick()  # fire immediately so the user sees data at once

    @Slot()
    def stop_poll(self) -> None:
        self._stop_poll_internal()
        self.log.emit("Polling stopped")
        self.poll_stopped.emit()

    def _stop_poll_internal(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._poll_req = None

    def _on_poll_tick(self) -> None:
        if self._poll_req is None:
            return
        self.do_read(self._poll_req)

    # --------------------------------------------------------------- #
    # Discovery scans (run to completion on this thread; cancel via Event)
    # --------------------------------------------------------------- #
    @Slot(object)
    def scan_slaves(self, req: "SlaveScanRequest") -> None:
        self._cancel.clear()
        self.log.emit(f"Scanning Unit IDs {req.start_unit}..{req.end_unit} ...")
        summary = discovery.scan_slaves(
            self._service, req.start_unit, req.end_unit, req.func, req.address,
            on_result=self.slave_found.emit,
            on_progress=self.slave_scan_progress.emit,
            is_cancelled=self._cancel.is_set,
        )
        cancelled = self._cancel.is_set()
        self.log.emit(
            f"Slave scan {'cancelled' if cancelled else 'done'}: "
            f"{len(summary.responding)} responding, "
            f"{len(summary.exceptions)} answered with exception")
        self.slave_scan_done.emit(summary)

    @Slot(object)
    def scan_registers(self, req: "RegisterScanRequest") -> None:
        self._cancel.clear()
        self.log.emit(
            f"Scanning {req.func.label} {req.start}..{req.end} on unit {req.unit} ...")
        regions = discovery.scan_registers(
            self._service, req.unit, req.func, req.start, req.end, req.block,
            on_result=self.region_found.emit,
            on_progress=self.register_scan_progress.emit,
            is_cancelled=self._cancel.is_set,
        )
        cancelled = self._cancel.is_set()
        self.log.emit(
            f"Register scan {'cancelled' if cancelled else 'done'}: "
            f"{len(regions)} responding block(s)")
        self.register_scan_done.emit(regions)

    # --------------------------------------------------------------- #
    # AI register-map extraction / validation
    # --------------------------------------------------------------- #
    @Slot(object)
    def do_extract(self, req: "ExtractRequest") -> None:
        try:
            profiles = ai_extractor.extract_profiles(
                req.provider, req.pdf_path, req.page_range, on_log=self.log.emit)
            self.extract_done.emit(profiles)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"Extraction failed: {exc}")
            self.extract_failed.emit(str(exc))

    @Slot(object)
    def do_validate(self, req: "ValidateRequest") -> None:
        self._cancel.clear()
        summary = ai_validation.validate_all(
            self._service, req.profiles, req.unit,
            on_progress=self.validate_progress.emit,
            is_cancelled=self._cancel.is_set,
        )
        self.log.emit(
            f"Validation done: {summary.get('verified', 0)} verified, "
            f"{summary.get('mismatch', 0)} mismatch, {summary.get('unread', 0)} unread")
        self.validate_done.emit(summary)

    # --------------------------------------------------------------- #
    def _log_result(self, label: str, address: int, result: OpResult) -> None:
        if result.ok:
            self.log.emit(f"{label} @ {address}: {len(result.values)} value(s) OK")
        else:
            self.log.emit(f"{label} @ {address}: {result.error}")
