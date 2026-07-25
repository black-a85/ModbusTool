"""Drive the real MainWindow (offscreen) through the discovery workflow to
verify the full GUI <-> worker wiring: connect, scan slaves, scan registers,
inspect suggestions, and apply one to the Manual tab."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QDeadlineTimer, QEventLoop, QTimer

from modbus_tool.main_window import MainWindow
from modbus_tool.formatting import DisplayFormat

HOST, PORT = "127.0.0.1", 5020
failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        failures.append(name)


def pump(app, ms):
    """Run the event loop for ms milliseconds."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(app, predicate, timeout_ms=6000):
    dl = QDeadlineTimer(timeout_ms)
    while not predicate() and not dl.hasExpired():
        pump(app, 50)
    return predicate()


def main():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()

    # connect via TCP to the simulator
    win.rb_tcp.setChecked(True)
    win.ed_host.setText(HOST)
    win.sp_port.setValue(PORT)
    win.sp_tcp_timeout.setValue(1.0)
    win._toggle_connect()
    ok = wait_until(app, lambda: win._connected)
    check("GUI connected", ok)

    # ---- slave scan 1..4 ------------------------------------------- #
    win.sp_scan_unit_start.setValue(1)
    win.sp_scan_unit_end.setValue(4)
    win._do_scan_slaves()
    ok = wait_until(app, lambda: not win._scanning and win.tbl_slaves.rowCount() == 4)
    check("slave table has 4 rows", win.tbl_slaves.rowCount() == 4,
          str(win.tbl_slaves.rowCount()))
    check("row 0 is unit 1 responding",
          win.tbl_slaves.item(0, 0).text() == "1"
          and win.tbl_slaves.item(0, 1).text() == "responding")

    # selecting the responding slave loads it into register-scan unit
    win.tbl_slaves.selectRow(0)
    pump(app, 100)
    check("selecting slave sets register unit", win.sp_rscan_unit.value() == 1,
          str(win.sp_rscan_unit.value()))

    # ---- register scan incl. the float at HR100 -------------------- #
    win.sp_rscan_start.setValue(100)
    win.sp_rscan_end.setValue(101)
    win.sp_rscan_block.setValue(2)
    win._do_scan_registers()
    ok = wait_until(app, lambda: not win._scanning and win.tbl_regions.rowCount() >= 1)
    check("region table populated", win.tbl_regions.rowCount() >= 1,
          str(win.tbl_regions.rowCount()))
    check("region status ok", win.tbl_regions.item(0, 2).text() == "ok",
          win.tbl_regions.item(0, 2).text())
    check("region carries a float suggestion",
          "Float" in win.tbl_regions.item(0, 4).text(),
          win.tbl_regions.item(0, 4).text())

    # ---- select the region -> suggestions table populates ---------- #
    win.tbl_regions.selectRow(0)
    pump(app, 150)
    check("suggestions populated", win.tbl_suggest.rowCount() >= 1,
          str(win.tbl_suggest.rowCount()))
    check("top suggestion is Float32",
          win.tbl_suggest.item(0, 0).text() == DisplayFormat.FLOAT32.value,
          win.tbl_suggest.item(0, 0).text())

    # ---- apply suggestion -> Manual tab reflects it ---------------- #
    win.tbl_suggest.selectRow(0)
    win._apply_suggestion()
    pump(app, 100)
    check("manual tab switched", win.tabs.currentIndex() == 0)
    check("manual format = Float32",
          win.cb_format.currentData() == DisplayFormat.FLOAT32,
          win.cb_format.currentText())
    check("manual word order = ABCD",
          win.cb_word_order.currentData() == "ABCD", win.cb_word_order.currentText())
    check("manual address = 100", win.sp_address.value() == 100,
          str(win.sp_address.value()))

    # ---- actually read + decode on the Manual tab (proves the combo-
    #      coercion decode bug is fixed) ---------------------------- #
    win.sp_count.setValue(2)
    win._do_read_once()
    ok = wait_until(app, lambda: win.table.rowCount() >= 1)
    val_text = win.table.item(0, 2).text() if win.table.rowCount() else ""
    check("manual tab decodes float ~3.14159",
          val_text and abs(float(val_text) - 3.14159) < 1e-3, f"showed '{val_text}'")

    win.close()
    pump(app, 100)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
