"""End-to-end smoke test against the running simulator (127.0.0.1:5020).

Exercises the ModbusService read/write paths and the formatting layer, then
builds the GUI headlessly (offscreen) to confirm it constructs and can connect.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.modbus_client import (
    ModbusService, TcpConfig,
    READ_HOLDING, READ_INPUT, READ_COILS, READ_DISCRETE,
    WRITE_REGISTER, WRITE_REGISTERS,
)
from modbus_tool.formatting import DisplayFormat, decode_registers, decode_bits

HOST, PORT, UNIT = "127.0.0.1", 5020, 1
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}  {detail}")
    if not cond:
        failures.append(name)


def main():
    svc = ModbusService()
    svc.connect_tcp(TcpConfig(host=HOST, port=PORT, timeout=2.0))
    print("Connected:", svc.description)

    # --- holding registers 0..9 -> 0,10,20,... --------------------- #
    r = svc.read(READ_HOLDING, 0, 10, UNIT)
    check("read holding ok", r.ok, r.error or "")
    check("holding values", r.ok and r.values == [i * 10 for i in range(10)], str(r.values))

    # --- input registers 0..4 -> 1000,1001,... --------------------- #
    r = svc.read(READ_INPUT, 0, 5, UNIT)
    check("input values", r.ok and r.values == [1000, 1001, 1002, 1003, 1004], str(r.values))

    # --- coils: even index ON -------------------------------------- #
    r = svc.read(READ_COILS, 0, 6, UNIT)
    check("coils ok", r.ok, r.error or "")
    check("coils pattern", r.ok and r.values[:4] == [True, False, True, False], str(r.values[:6]))

    # --- discrete inputs: every 3rd ON ----------------------------- #
    r = svc.read(READ_DISCRETE, 0, 6, UNIT)
    check("discrete pattern", r.ok and r.values[0] is True and r.values[3] is True, str(r.values[:6]))

    # --- float32 at HR 100..101 = 3.14159 -------------------------- #
    r = svc.read(READ_HOLDING, 100, 2, UNIT)
    rows = decode_registers(r.values, 100, DisplayFormat.FLOAT32, "big")
    fval = float(rows[0].value_text)
    check("float32 decode", abs(fval - 3.14159) < 1e-4, rows[0].value_text)

    # --- u32 at HR 110..111 = 0x00012345 = 74565 ------------------- #
    r = svc.read(READ_HOLDING, 110, 2, UNIT)
    rows = decode_registers(r.values, 110, DisplayFormat.U32, "big")
    check("u32 decode", rows[0].value_text == "74565", rows[0].value_text)

    # --- signed 16 wrap: write 0xFFFF then read as signed ---------- #
    w = svc.write(WRITE_REGISTER, 50, [0xFFFF], UNIT)
    check("write single ok", w.ok, w.error or "")
    r = svc.read(READ_HOLDING, 50, 1, UNIT)
    rows = decode_registers(r.values, 50, DisplayFormat.S16, "big")
    check("signed16 -1", rows[0].value_text == "-1", rows[0].value_text)

    # --- write multiple then read back ----------------------------- #
    w = svc.write(WRITE_REGISTERS, 60, [111, 222, 333], UNIT)
    check("write multi ok", w.ok, w.error or "")
    r = svc.read(READ_HOLDING, 60, 3, UNIT)
    check("write multi readback", r.values == [111, 222, 333], str(r.values))

    # --- exception path: read absurd address ----------------------- #
    r = svc.read(READ_HOLDING, 60000, 20, UNIT)
    check("bad address is error", not r.ok, r.error or "")

    svc.close()

    # --- GUI constructs headlessly + connects ---------------------- #
    from PySide6.QtWidgets import QApplication
    from modbus_tool.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.ed_host.setText(HOST)
    win.sp_port.setValue(PORT)
    win.show()
    app.processEvents()
    check("GUI built", win.table.columnCount() == 3)
    check("GUI has functions", win.cb_function.count() == 8)
    win.close()
    app.processEvents()

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
