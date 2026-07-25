"""Exercise the threaded worker + polling against the simulator."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QThread, QTimer, QObject, Signal
from PySide6.QtWidgets import QApplication

from modbus_tool.modbus_client import TcpConfig, READ_HOLDING
from modbus_tool.worker import ModbusWorker, ReadRequest, PollRequest

HOST, PORT, UNIT = "127.0.0.1", 5020, 1


class Driver(QObject):
    do_connect = Signal(object)
    do_start_poll = Signal(object)
    do_stop_poll = Signal()
    do_disconnect = Signal()


def main():
    app = QApplication.instance() or QApplication([])
    thread = QThread()
    worker = ModbusWorker()
    worker.moveToThread(thread)
    drv = Driver()
    drv.do_connect.connect(worker.do_connect)
    drv.do_start_poll.connect(worker.start_poll)
    drv.do_stop_poll.connect(worker.stop_poll)
    drv.do_disconnect.connect(worker.do_disconnect)
    thread.start()

    state = {"connected": False, "reads": 0, "last": None}
    worker.connected.connect(lambda d: state.update(connected=True))
    worker.read_done.connect(lambda res, req, seq: (
        state.update(reads=state["reads"] + 1, last=res.values)))

    drv.do_connect.emit(TcpConfig(host=HOST, port=PORT, timeout=2.0))

    # let it connect, then poll ~4 times at 150ms
    QTimer.singleShot(300, lambda: drv.do_start_poll.emit(
        PollRequest(read=ReadRequest(READ_HOLDING, 0, 5, UNIT), interval_ms=150)))
    QTimer.singleShot(1100, drv.do_stop_poll.emit)

    def finish():
        drv.do_disconnect.emit()
        thread.quit()
        thread.wait(2000)
        app.quit()
    QTimer.singleShot(1400, finish)

    app.exec()

    ok = state["connected"] and state["reads"] >= 3 and state["last"] == [0, 10, 20, 30, 40]
    print(f"connected={state['connected']} poll_reads={state['reads']} last={state['last']}")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
