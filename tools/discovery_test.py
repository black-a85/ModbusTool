"""Tests for the discovery layer against the simulator (127.0.0.1:5020).

Covers slave scan, register scan, and the format analyzer's ranking on known
float / u32 / integer data.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.modbus_client import (
    ModbusService, TcpConfig, READ_HOLDING, READ_INPUT,
)
from modbus_tool import discovery
from modbus_tool.formatting import DisplayFormat

HOST, PORT = "127.0.0.1", 5020
failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        failures.append(name)


def main():
    svc = ModbusService()
    svc.connect_tcp(TcpConfig(host=HOST, port=PORT, timeout=1.0))

    # ---- slave scan ------------------------------------------------ #
    # Unit 1 serves data; this simulator answers other unit IDs with an
    # exception (code 4), so they land in the "exception" bucket, not
    # "responding". That is the honest three-way classification.
    found = []
    summary = discovery.scan_slaves(
        svc, 1, 5, READ_HOLDING, 0,
        on_result=found.append, on_progress=lambda a, b: None,
        is_cancelled=lambda: False,
    )
    responding = sorted(p.unit for p in summary.responding)
    check("slave scan: unit 1 responds with data", responding == [1],
          f"responding={responding}")
    check("slave scan reports 5 probed", len(found) == 5, f"{len(found)}")
    statuses = {f.unit: f.status for f in found}
    check("unit 1 responding", statuses.get(1) == "responding", str(statuses))
    check("unit 2 answered w/ exception", statuses.get(2) == "exception", str(statuses))

    # ---- register scan: holding 0..119 in blocks of 10 ------------- #
    regions = []
    ok = discovery.scan_registers(
        svc, 1, READ_HOLDING, 0, 119, 10,
        on_result=regions.append, on_progress=lambda a, b: None,
        is_cancelled=lambda: False,
    )
    check("register scan: 12 blocks probed", len(regions) == 12, f"{len(regions)}")
    check("register scan: all responded ok", len(ok) == 12, f"ok={len(ok)}")
    first = ok[0]
    check("first block values", first.values[:3] == [0, 10, 20], str(first.values[:3]))
    check("blocks carry a suggestion", bool(first.suggestion), first.suggestion)

    # ---- exception region: scan a range that includes bad addresses  #
    regions2 = []
    discovery.scan_registers(
        svc, 1, READ_HOLDING, 59990, 60019, 10,
        on_result=regions2.append, on_progress=lambda a, b: None,
        is_cancelled=lambda: False,
    )
    has_exc = any(r.status == "exception" for r in regions2)
    check("out-of-range yields exception status", has_exc,
          str([(r.address, r.status) for r in regions2]))

    # ---- format analyzer: float at HR 100..101 = 3.14159 ----------- #
    r = svc.read(READ_HOLDING, 100, 2, 1)
    sugg = discovery.analyze_block(r.values)
    top = sugg[0]
    check("float block -> FLOAT32 top", top.fmt is DisplayFormat.FLOAT32,
          f"{top.fmt.value} / {top.word_order}")
    check("float block -> ABCD order", top.word_order == "ABCD", top.word_order)
    check("float sample ~3.14159", abs(float(top.sample) - 3.14159) < 1e-3, top.sample)
    check("float confidence High", top.confidence >= 0.8, f"{top.confidence:.2f}")

    # ---- analyzer: plain small ints -> u16 wins over float --------- #
    r = svc.read(READ_HOLDING, 0, 10, 1)
    sugg = discovery.analyze_block(r.values)
    check("int block -> U16 top", sugg[0].fmt is DisplayFormat.RAW_U16,
          sugg[0].fmt.value)
    has_float = any(s.fmt is DisplayFormat.FLOAT32 for s in sugg)
    check("int block: float not suggested", not has_float,
          str([s.fmt.value for s in sugg]))

    # ---- analyzer: signed negatives -> s16 present ----------------- #
    from modbus_tool.modbus_client import WRITE_REGISTERS
    svc.write(WRITE_REGISTERS, 70, [0xFFFF, 0xFFFE, 0xFFFD, 0xFFFC], 1)
    r = svc.read(READ_HOLDING, 70, 4, 1)
    sugg = discovery.analyze_block(r.values)
    has_s16 = any(s.fmt is DisplayFormat.S16 for s in sugg)
    check("negative block: s16 suggested", has_s16,
          str([s.fmt.value for s in sugg]))

    svc.close()

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
