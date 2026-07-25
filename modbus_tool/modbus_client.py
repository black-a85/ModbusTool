"""Modbus transport + operation wrapper around pymodbus.

This module isolates all pymodbus specifics so the GUI never touches the
library directly. It supports both TCP and serial (RTU) transports and the
common read/write function codes. Everything here is blocking and is meant to
be driven from a background worker thread (see worker.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ModbusException, ConnectionException


# --------------------------------------------------------------------------- #
# Function codes
# --------------------------------------------------------------------------- #
class DataKind(str, Enum):
    BITS = "bits"          # coils / discrete inputs -> booleans
    REGISTERS = "registers"  # holding / input -> 16-bit words


@dataclass(frozen=True)
class ModbusFunction:
    code: int
    label: str
    access: str            # "read" or "write"
    kind: DataKind
    multi: bool = False    # write functions: single vs multiple


# Read functions
READ_COILS = ModbusFunction(1, "FC01 - Read Coils", "read", DataKind.BITS)
READ_DISCRETE = ModbusFunction(2, "FC02 - Read Discrete Inputs", "read", DataKind.BITS)
READ_HOLDING = ModbusFunction(3, "FC03 - Read Holding Registers", "read", DataKind.REGISTERS)
READ_INPUT = ModbusFunction(4, "FC04 - Read Input Registers", "read", DataKind.REGISTERS)

# Write functions
WRITE_COIL = ModbusFunction(5, "FC05 - Write Single Coil", "write", DataKind.BITS, multi=False)
WRITE_REGISTER = ModbusFunction(6, "FC06 - Write Single Register", "write", DataKind.REGISTERS, multi=False)
WRITE_COILS = ModbusFunction(15, "FC15 - Write Multiple Coils", "write", DataKind.BITS, multi=True)
WRITE_REGISTERS = ModbusFunction(16, "FC16 - Write Multiple Registers", "write", DataKind.REGISTERS, multi=True)

READ_FUNCTIONS = [READ_HOLDING, READ_INPUT, READ_COILS, READ_DISCRETE]
WRITE_FUNCTIONS = [WRITE_REGISTER, WRITE_REGISTERS, WRITE_COIL, WRITE_COILS]
ALL_FUNCTIONS = READ_FUNCTIONS + WRITE_FUNCTIONS


# Standard Modbus exception codes -> human text
_EXCEPTION_TEXT = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Slave Device Failure",
    5: "Acknowledge",
    6: "Slave Device Busy",
    7: "Negative Acknowledge",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed To Respond",
}


# --------------------------------------------------------------------------- #
# Transport configuration
# --------------------------------------------------------------------------- #
@dataclass
class TcpConfig:
    host: str = "192.168.1.10"
    port: int = 502
    timeout: float = 2.0


@dataclass
class SerialConfig:
    port: str = "COM1"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"       # N, E, O
    stopbits: int = 1
    timeout: float = 2.0


# --------------------------------------------------------------------------- #
# Operation result
# --------------------------------------------------------------------------- #
@dataclass
class OpResult:
    ok: bool
    kind: DataKind
    values: list = field(default_factory=list)   # ints (registers) or bools (coils)
    error: Optional[str] = None
    is_modbus_exception: bool = False            # True = device answered with an exception
    exception_code: Optional[int] = None


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class ModbusService:
    """Owns a single pymodbus client. Not thread-safe; use from one thread."""

    def __init__(self):
        self._client = None
        self._desc = ""

    # -- lifecycle -------------------------------------------------------- #
    def connect_tcp(self, cfg: TcpConfig) -> None:
        self.close()
        self._client = ModbusTcpClient(cfg.host, port=cfg.port, timeout=cfg.timeout)
        self._desc = f"TCP {cfg.host}:{cfg.port}"
        if not self._client.connect():
            raise ConnectionException(f"Could not connect to {self._desc}")

    def connect_serial(self, cfg: SerialConfig) -> None:
        self.close()
        self._client = ModbusSerialClient(
            cfg.port,
            baudrate=cfg.baudrate,
            bytesize=cfg.bytesize,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            timeout=cfg.timeout,
        )
        self._desc = f"Serial {cfg.port} {cfg.baudrate}-{cfg.bytesize}{cfg.parity}{cfg.stopbits}"
        if not self._client.connect():
            raise ConnectionException(f"Could not open serial port {cfg.port}")

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    @property
    def description(self) -> str:
        return self._desc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    # -- read ------------------------------------------------------------- #
    def read(self, func: ModbusFunction, address: int, count: int, unit: int) -> OpResult:
        if not self.connected:
            return OpResult(False, func.kind, error="Not connected")
        try:
            if func is READ_HOLDING:
                resp = self._client.read_holding_registers(address, count=count, device_id=unit)
            elif func is READ_INPUT:
                resp = self._client.read_input_registers(address, count=count, device_id=unit)
            elif func is READ_COILS:
                resp = self._client.read_coils(address, count=count, device_id=unit)
            elif func is READ_DISCRETE:
                resp = self._client.read_discrete_inputs(address, count=count, device_id=unit)
            else:
                return OpResult(False, func.kind, error=f"{func.label} is not a read function")
        except ConnectionException as exc:
            return OpResult(False, func.kind, error=f"Connection error: {exc}")
        except ModbusException as exc:
            return OpResult(False, func.kind, error=f"Modbus error: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            return OpResult(False, func.kind, error=f"{type(exc).__name__}: {exc}")

        return self._normalize_read(func, resp, count)

    def _normalize_read(self, func: ModbusFunction, resp, count: int) -> OpResult:
        if resp is None:
            return OpResult(False, func.kind, error="No response (timeout)")
        if resp.isError():
            code = getattr(resp, "exception_code", None)
            if code is not None:
                text = _EXCEPTION_TEXT.get(code, f"Exception code {code}")
                return OpResult(
                    False, func.kind,
                    error=f"Device exception: {text}",
                    is_modbus_exception=True, exception_code=code,
                )
            return OpResult(False, func.kind, error=f"Error response: {resp}")

        if func.kind is DataKind.REGISTERS:
            values = list(resp.registers)[:count]
        else:
            values = list(resp.bits)[:count]
        return OpResult(True, func.kind, values=values)

    # -- write ------------------------------------------------------------ #
    def write(self, func: ModbusFunction, address: int, values: list, unit: int) -> OpResult:
        if not self.connected:
            return OpResult(False, func.kind, error="Not connected")
        try:
            if func is WRITE_REGISTER:
                resp = self._client.write_register(address, int(values[0]), device_id=unit)
            elif func is WRITE_REGISTERS:
                resp = self._client.write_registers(address, [int(v) for v in values], device_id=unit)
            elif func is WRITE_COIL:
                resp = self._client.write_coil(address, bool(values[0]), device_id=unit)
            elif func is WRITE_COILS:
                resp = self._client.write_coils(address, [bool(v) for v in values], device_id=unit)
            else:
                return OpResult(False, func.kind, error=f"{func.label} is not a write function")
        except ConnectionException as exc:
            return OpResult(False, func.kind, error=f"Connection error: {exc}")
        except ModbusException as exc:
            return OpResult(False, func.kind, error=f"Modbus error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return OpResult(False, func.kind, error=f"{type(exc).__name__}: {exc}")

        if resp is None:
            return OpResult(False, func.kind, error="No response (timeout)")
        if resp.isError():
            code = getattr(resp, "exception_code", None)
            if code is not None:
                text = _EXCEPTION_TEXT.get(code, f"Exception code {code}")
                return OpResult(False, func.kind, error=f"Device exception: {text}",
                                is_modbus_exception=True, exception_code=code)
            return OpResult(False, func.kind, error=f"Error response: {resp}")
        return OpResult(True, func.kind, values=list(values))
