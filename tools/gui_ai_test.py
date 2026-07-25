"""Drive the Register Map tab (offscreen) through extract -> validate -> save/load
-> send-to-manual, using a MockProvider so no real LLM is needed."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer, QDeadlineTimer

from modbus_tool.main_window import MainWindow
from modbus_tool.ai.providers import MockProvider
from modbus_tool.formatting import DisplayFormat

HOST, PORT = "127.0.0.1", 5020
failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        failures.append(name)


def pump(app, ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(app, predicate, timeout_ms=8000):
    dl = QDeadlineTimer(timeout_ms)
    while not predicate() and not dl.hasExpired():
        pump(app, 50)
    return predicate()


def make_pdf(path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("Helvetica", 11)
    c.drawString(50, 720, "40001 Ramp uint16 ; 40101 Pi float32 bar")
    c.save()


MOCK = [
    {"printed_register": "40001", "table": "holding", "address_base": "modicon",
     "data_type": "uint16", "name": "Ramp Value 0"},
    {"printed_register": "40011", "table": "holding", "address_base": "modicon",
     "data_type": "uint16", "name": "Supply Temp", "scale": 0.1, "unit": "degC"},
    {"printed_register": "40101", "table": "holding", "address_base": "modicon",
     "data_type": "float32", "name": "Pi Constant", "unit": "bar"},
    {"printed_register": "49999", "table": "holding", "address_base": "modicon",
     "data_type": "uint16", "name": "Ghost"},
]


def main():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()

    # connect
    win.rb_tcp.setChecked(True)
    win.ed_host.setText(HOST)
    win.sp_port.setValue(PORT)
    win.sp_tcp_timeout.setValue(1.0)
    win._toggle_connect()
    check("connected", wait_until(app, lambda: win._connected))

    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "map.pdf")
        make_pdf(pdf)

        # inject mock provider + go to the Register Map tab
        win._build_provider = lambda: MockProvider(MOCK)
        win.ed_pdf.setText(pdf)
        win.tabs.setCurrentIndex(2)

        # ---- extract ---------------------------------------------- #
        win._do_extract()
        ok = wait_until(app, lambda: not win._busy_ai
                        and win.tbl_profiles.rowCount() == len(MOCK))
        check("extract populated table", win.tbl_profiles.rowCount() == len(MOCK),
              str(win.tbl_profiles.rowCount()))
        # Addr column (index 3) computed for 40011 -> 10
        names = {win.tbl_profiles.item(r, 4).text(): r for r in range(win.tbl_profiles.rowCount())}
        r_supply = names["Supply Temp"]
        check("40011 -> addr 10", win.tbl_profiles.item(r_supply, 3).text() == "10",
              win.tbl_profiles.item(r_supply, 3).text())

        # ---- validate against the simulator ----------------------- #
        win._do_validate()
        ok = wait_until(app, lambda: not win._busy_ai
                        and win.tbl_profiles.item(names["Pi Constant"], 11).text() != "unverified")
        r_pi = names["Pi Constant"]
        check("Pi verified", win.tbl_profiles.item(r_pi, 11).text() == "verified",
              win.tbl_profiles.item(r_pi, 11).text())
        check("Pi live ~3.14 bar", win.tbl_profiles.item(r_pi, 12).text().startswith("3.14"),
              win.tbl_profiles.item(r_pi, 12).text())
        check("Supply Temp scaled 10 degC",
              win.tbl_profiles.item(r_supply, 12).text().startswith("10"),
              win.tbl_profiles.item(r_supply, 12).text())
        r_ghost = names["Ghost"]
        check("Ghost unread", win.tbl_profiles.item(r_ghost, 11).text() == "unread",
              win.tbl_profiles.item(r_ghost, 11).text())

        # ---- edit a cell re-normalizes ---------------------------- #
        win.tbl_profiles.item(r_supply, 0).setText("40021")  # printed register
        pump(app, 100)
        check("edit re-normalizes addr to 20",
              win.tbl_profiles.item(r_supply, 3).text() == "20",
              win.tbl_profiles.item(r_supply, 3).text())

        # ---- save / load ------------------------------------------ #
        jpath = os.path.join(tmp, "dev.json")
        win._do_save_profile = lambda: __import__("modbus_tool.ai.profile_store",
                                                  fromlist=["save"]).save(jpath, win._profiles)
        win._do_save_profile()
        check("profile file written", os.path.isfile(jpath))
        win._profiles = []
        win.tbl_profiles.setRowCount(0)
        from modbus_tool.ai import profile_store
        _n, loaded = profile_store.load(jpath)
        win._profiles = loaded
        win._render_profiles()
        check("reload restores rows", win.tbl_profiles.rowCount() == len(MOCK),
              str(win.tbl_profiles.rowCount()))

        # ---- send selected to Manual tab -------------------------- #
        names2 = {win.tbl_profiles.item(r, 4).text(): r
                  for r in range(win.tbl_profiles.rowCount())}
        win.tbl_profiles.selectRow(names2["Pi Constant"])
        win._rm_send_to_manual()
        pump(app, 100)
        check("manual tab active", win.tabs.currentIndex() == 0)
        check("manual format float32",
              win.cb_format.currentData() == DisplayFormat.FLOAT32,
              win.cb_format.currentText())
        check("manual address 100", win.sp_address.value() == 100,
              str(win.sp_address.value()))

    win.close()
    pump(app, 100)

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
