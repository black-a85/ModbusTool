"""ModbusTool entry point.

Run:  python main.py
Self-test the packaged build:  ModbusTool.exe --selftest <output.txt>

ModbusTool - a cross-platform Modbus master GUI.
Copyright (C) 2026 Ibrahim Abdullatif

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. This program is distributed WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details. You should have received a copy of
the GNU General Public License along with this program (see the LICENSE file);
if not, see <https://www.gnu.org/licenses/>.
"""

import sys


def _selftest() -> int:
    """Import every (including lazily-loaded) dependency and report.

    Used to verify a PyInstaller build bundled everything. Writes the result to
    the file path following --selftest (a windowed exe has no console), and
    exits 0 on success, 2 on any failure."""
    import importlib
    mods = [
        "PySide6.QtWidgets", "pymodbus.client", "serial.tools.list_ports",
        "psutil", "pdfplumber", "pypdfium2", "pypdf", "anthropic", "openai",
        "modbus_tool.main_window", "modbus_tool.ai.providers",
    ]
    results = {}
    for m in mods:
        try:
            importlib.import_module(m)
            results[m] = "ok"
        except Exception as exc:  # noqa: BLE001
            results[m] = f"FAIL: {exc}"
    text = "\n".join(f"{k}: {v}" for k, v in results.items())
    idx = sys.argv.index("--selftest")
    out = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    return 0 if all(v == "ok" for v in results.values()) else 2


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    from modbus_tool.main_window import run
    run()
