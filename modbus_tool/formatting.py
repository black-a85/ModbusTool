"""Register value decoding.

Modbus registers are raw 16-bit words. How they should be interpreted is up to
the device's memory map, so the user picks a display format. This module turns a
list of raw 16-bit registers into displayable rows.

Byte/word order (for 32-bit / float values that span two registers).
Given the two registers' four bytes A B (first reg) and C D (second reg):
  * "ABCD" -> big-endian, first register is the high word (Modbus standard)
  * "CDAB" -> word-swapped (first register is the low word)
  * "BADC" -> byte-swapped within each word
  * "DCBA" -> full little-endian (bytes fully reversed)
These four cover the "inverted or not" cases seen across real devices.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum


class DisplayFormat(str, Enum):
    RAW_U16 = "Unsigned 16-bit"
    S16 = "Signed 16-bit"
    HEX16 = "Hex 16-bit"
    BINARY16 = "Binary 16-bit"
    U32 = "Unsigned 32-bit"
    S32 = "Signed 32-bit"
    FLOAT32 = "Float 32-bit"
    HEX32 = "Hex 32-bit"


# formats that consume two registers per displayed value
_WIDE_FORMATS = {
    DisplayFormat.U32, DisplayFormat.S32,
    DisplayFormat.FLOAT32, DisplayFormat.HEX32,
}


def _as_format(fmt) -> DisplayFormat:
    """Normalize a format that may have been coerced to a plain str.

    DisplayFormat is a str-Enum, and Qt's QComboBox stores item data as a plain
    str, stripping the enum identity. Everything downstream relies on identity
    (`fmt is DisplayFormat.X`), so we re-hydrate the enum at the boundary.
    """
    return fmt if isinstance(fmt, DisplayFormat) else DisplayFormat(fmt)


def is_wide(fmt) -> bool:
    return _as_format(fmt) in _WIDE_FORMATS


# UI-facing list of (label, code) for the byte/word order selector
WORD_ORDERS = [
    ("ABCD  (big-endian)", "ABCD"),
    ("CDAB  (word-swapped)", "CDAB"),
    ("BADC  (byte-swapped)", "BADC"),
    ("DCBA  (little-endian)", "DCBA"),
]


def reorder_bytes(hi: int, lo: int, order: str) -> bytes:
    """Turn two 16-bit registers into 4 ordered bytes per the given scheme."""
    a = (hi >> 8) & 0xFF  # A
    b = hi & 0xFF         # B
    c = (lo >> 8) & 0xFF  # C
    d = lo & 0xFF         # D
    table = {
        "ABCD": (a, b, c, d),
        "CDAB": (c, d, a, b),
        "BADC": (b, a, d, c),
        "DCBA": (d, c, b, a),
    }
    return bytes(table.get(order, table["ABCD"]))


@dataclass
class DecodedRow:
    address: int          # protocol address of the first register in this row
    registers: list       # the raw 16-bit register(s) that make up this row
    raw_text: str         # raw hex of the underlying registers, e.g. "0x1A2B"
    value_text: str       # the decoded value shown to the user


def decode_registers(
    registers: list[int],
    start_address: int,
    fmt: DisplayFormat,
    word_order: str = "ABCD",
) -> list[DecodedRow]:
    """Decode raw registers into display rows according to fmt."""
    fmt = _as_format(fmt)
    rows: list[DecodedRow] = []

    if not is_wide(fmt):
        for i, reg in enumerate(registers):
            reg &= 0xFFFF
            rows.append(DecodedRow(
                address=start_address + i,
                registers=[reg],
                raw_text=f"0x{reg:04X}",
                value_text=_decode_16(reg, fmt),
            ))
        return rows

    # wide (32-bit) formats: pair registers up
    i = 0
    while i + 1 < len(registers):
        hi = registers[i] & 0xFFFF
        lo = registers[i + 1] & 0xFFFF
        raw = reorder_bytes(hi, lo, word_order)
        rows.append(DecodedRow(
            address=start_address + i,
            registers=[hi, lo],
            raw_text=f"0x{hi:04X} 0x{lo:04X}",
            value_text=_decode_32(raw, fmt),
        ))
        i += 2

    # leftover odd register (can't form a 32-bit value) - show raw
    if i < len(registers):
        reg = registers[i] & 0xFFFF
        rows.append(DecodedRow(
            address=start_address + i,
            registers=[reg],
            raw_text=f"0x{reg:04X}",
            value_text=f"(half word) 0x{reg:04X}",
        ))
    return rows


def _decode_16(reg: int, fmt: DisplayFormat) -> str:
    if fmt is DisplayFormat.RAW_U16:
        return str(reg)
    if fmt is DisplayFormat.S16:
        return str(struct.unpack(">h", struct.pack(">H", reg))[0])
    if fmt is DisplayFormat.HEX16:
        return f"0x{reg:04X}"
    if fmt is DisplayFormat.BINARY16:
        return format(reg, "016b")
    return str(reg)


def _decode_32(raw: bytes, fmt: DisplayFormat) -> str:
    """Decode 4 already-ordered bytes into the requested 32-bit format."""
    if fmt is DisplayFormat.U32:
        return str(struct.unpack(">I", raw)[0])
    if fmt is DisplayFormat.S32:
        return str(struct.unpack(">i", raw)[0])
    if fmt is DisplayFormat.HEX32:
        return f"0x{struct.unpack('>I', raw)[0]:08X}"
    if fmt is DisplayFormat.FLOAT32:
        f = struct.unpack(">f", raw)[0]
        return f"{f:.6g}"
    return str(struct.unpack(">I", raw)[0])


def decode_bits(bits: list[bool], start_address: int) -> list[DecodedRow]:
    """Coils / discrete inputs are simple booleans."""
    rows: list[DecodedRow] = []
    for i, b in enumerate(bits):
        rows.append(DecodedRow(
            address=start_address + i,
            registers=[1 if b else 0],
            raw_text="1" if b else "0",
            value_text="ON" if b else "OFF",
        ))
    return rows
