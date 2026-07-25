"""Auto-discovery logic (iteration 2).

Three independent, testable pieces of pure logic that drive a ModbusService:

  1. scan_slaves()     - which Unit IDs answer on the bus / gateway.
  2. scan_registers()  - for a slave, which address blocks return data vs errors.
  3. analyze_block()   - given raw registers, rank the likely display formats
                         (16/32-bit, signed/unsigned, float, byte/word order).

Each scan takes callbacks so the worker thread can stream progress, collect
results, and cancel mid-scan without this module knowing about Qt.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Callable

from .modbus_client import ModbusService, ModbusFunction, DataKind, READ_HOLDING
from .formatting import DisplayFormat, reorder_bytes


# --------------------------------------------------------------------------- #
# Slave discovery
# --------------------------------------------------------------------------- #
@dataclass
class SlaveResult:
    unit: int
    status: str          # 'responding' | 'exception' | 'no-response'
    detail: str


@dataclass
class SlaveScanSummary:
    responding: list = field(default_factory=list)   # answered with data (green)
    exceptions: list = field(default_factory=list)   # answered with an exception (amber)

    @property
    def answered(self) -> int:
        return len(self.responding) + len(self.exceptions)


# Exception codes that specifically mean "the target device did not answer"
# (a Modbus gateway/bridge reporting an empty slot), i.e. treat as absent.
_GATEWAY_ABSENT_CODES = {10, 11}


def scan_slaves(
    service: ModbusService,
    start_unit: int,
    end_unit: int,
    func: ModbusFunction,
    address: int,
    on_result: Callable[[SlaveResult], None],
    on_progress: Callable[[int, int], None],
    is_cancelled: Callable[[], bool],
) -> SlaveScanSummary:
    """Probe each Unit ID with a 1-item read.

    Classification (deliberately three-way, because "present" is topology
    dependent):
      * responding  - got valid data. Definitely present.
      * exception   - the device answered with a Modbus exception. On a serial
                      bus this means present (it just rejected the probe); the
                      caller decides how to treat it. Gateway codes 10/11 are
                      the exception: they mean the target is absent.
      * no-response - timeout / connection error. Absent (or wrong settings).
    """
    total = end_unit - start_unit + 1
    summary = SlaveScanSummary()
    for idx, unit in enumerate(range(start_unit, end_unit + 1)):
        if is_cancelled():
            break
        r = service.read(func, address, 1, unit)
        if r.ok:
            res = SlaveResult(unit, "responding", f"{func.label} @ {address}: OK")
            summary.responding.append(res)
        elif r.is_modbus_exception and r.exception_code not in _GATEWAY_ABSENT_CODES:
            res = SlaveResult(unit, "exception", f"answered with: {r.error}")
            summary.exceptions.append(res)
        else:
            res = SlaveResult(unit, "no-response", r.error or "no response")
        on_result(res)
        on_progress(idx + 1, total)
    return summary


# --------------------------------------------------------------------------- #
# Register discovery
# --------------------------------------------------------------------------- #
@dataclass
class RegionResult:
    address: int
    count: int
    status: str          # 'ok' | 'exception' | 'no-response'
    values: list = field(default_factory=list)
    detail: str = ""
    suggestion: str = ""  # top format guess (register reads only)


def scan_registers(
    service: ModbusService,
    unit: int,
    func: ModbusFunction,
    start: int,
    end: int,
    block: int,
    on_result: Callable[[RegionResult], None],
    on_progress: Callable[[int, int], None],
    is_cancelled: Callable[[], bool],
) -> list[RegionResult]:
    """Walk [start, end] in blocks, recording which blocks respond with data."""
    block = max(1, min(block, 25))
    addresses = list(range(start, end + 1, block))
    total = len(addresses)
    ok_regions: list[RegionResult] = []

    for idx, addr in enumerate(addresses):
        if is_cancelled():
            break
        count = min(block, end - addr + 1)
        r = service.read(func, addr, count, unit)
        if r.ok:
            reg = RegionResult(addr, count, "ok", list(r.values),
                               detail=f"{len(r.values)} value(s)")
            if func.kind is DataKind.REGISTERS and r.values:
                top = analyze_block(r.values)
                if top:
                    reg.suggestion = top[0].short()
            ok_regions.append(reg)
            on_result(reg)
        elif r.is_modbus_exception:
            on_result(RegionResult(addr, count, "exception", detail=r.error or ""))
        else:
            on_result(RegionResult(addr, count, "no-response", detail=r.error or ""))
        on_progress(idx + 1, total)
    return ok_regions


# --------------------------------------------------------------------------- #
# Smart format detection
# --------------------------------------------------------------------------- #
@dataclass
class FormatSuggestion:
    fmt: DisplayFormat
    word_order: str        # "" for 16-bit formats
    confidence: float      # 0..1
    sample: str            # decoded value of the first element
    reason: str

    def confidence_label(self) -> str:
        if self.confidence >= 0.8:
            return "High"
        if self.confidence >= 0.6:
            return "Medium"
        return "Low"

    def short(self) -> str:
        order = f" / {self.word_order}" if self.word_order else ""
        return f"{self.fmt.value}{order} ({self.confidence_label()})"


# plausible magnitude window for "real" engineering float values
_FLOAT_LO, _FLOAT_HI = 1e-3, 1e7


def _float_sanity(floats: list[float]) -> float:
    """Fraction of values that look like real, sensibly-scaled floats."""
    if not floats:
        return 0.0
    good = 0
    for f in floats:
        if not math.isfinite(f):
            continue
        af = abs(f)
        if f == 0.0 or (_FLOAT_LO <= af <= _FLOAT_HI):
            good += 1
    return good / len(floats)


def _pairs(regs: list[int]):
    for i in range(0, len(regs) - 1, 2):
        yield regs[i] & 0xFFFF, regs[i + 1] & 0xFFFF


def analyze_block(regs: list[int]) -> list[FormatSuggestion]:
    """Rank likely display formats for a block of raw 16-bit registers."""
    if not regs:
        return []
    out: list[FormatSuggestion] = []

    # ---- 16-bit unsigned -------------------------------------------- #
    all_pos = all(r <= 0x7FFF for r in regs)
    u16_conf = 0.55 + (0.05 if all_pos else 0.0)
    out.append(FormatSuggestion(
        DisplayFormat.RAW_U16, "", u16_conf,
        sample=str(regs[0] & 0xFFFF),
        reason="Every register is a valid unsigned 16-bit value.",
    ))

    # ---- 16-bit signed ---------------------------------------------- #
    signed_vals = [struct.unpack(">h", struct.pack(">H", r & 0xFFFF))[0] for r in regs]
    neg = [v for v in signed_vals if v < 0]
    small_neg = [v for v in neg if abs(v) < 1000]
    if neg:
        frac = len(small_neg) / len(regs)
        s16_conf = 0.5 + 0.35 * frac
        out.append(FormatSuggestion(
            DisplayFormat.S16, "", s16_conf,
            sample=str(signed_vals[0]),
            reason=f"{len(neg)} register(s) have the high bit set and read as "
                   f"small negative numbers when signed.",
        ))

    # ---- float32, all 4 byte/word orders ---------------------------- #
    best_float = None
    for order in ("ABCD", "CDAB", "BADC", "DCBA"):
        floats = [struct.unpack(">f", reorder_bytes(hi, lo, order))[0]
                  for hi, lo in _pairs(regs)]
        if not floats:
            break
        sanity = _float_sanity(floats)
        if sanity < 0.5:
            continue
        conf = 0.4 + 0.6 * sanity
        cand = FormatSuggestion(
            DisplayFormat.FLOAT32, order, conf,
            sample=f"{floats[0]:.6g}",
            reason=f"Register pairs decode to well-scaled floating-point values "
                   f"with byte order {order} ({int(sanity * 100)}% plausible).",
        )
        if best_float is None or cand.confidence > best_float.confidence:
            best_float = cand
    if best_float:
        out.append(best_float)

    # ---- 32-bit unsigned / signed (ABCD, CDAB) ---------------------- #
    if len(regs) >= 2:
        for order in ("ABCD", "CDAB"):
            u32 = [struct.unpack(">I", reorder_bytes(hi, lo, order))[0]
                   for hi, lo in _pairs(regs)]
            # modest confidence: plausible if not absurdly large
            reasonable = sum(1 for v in u32 if v <= 100_000_000) / len(u32)
            conf = 0.3 + 0.15 * reasonable
            out.append(FormatSuggestion(
                DisplayFormat.U32, order, conf,
                sample=str(u32[0]),
                reason=f"Register pairs combine into 32-bit integers (order {order}).",
            ))
            # signed 32 only if any high bit set
            if any(v & 0x80000000 for v in u32):
                s32 = [struct.unpack(">i", struct.pack(">I", v))[0] for v in u32]
                out.append(FormatSuggestion(
                    DisplayFormat.S32, order, conf - 0.05,
                    sample=str(s32[0]),
                    reason=f"Some pairs have the top bit set; may be signed 32-bit "
                           f"(order {order}).",
                ))

    out.sort(key=lambda s: s.confidence, reverse=True)
    return out
