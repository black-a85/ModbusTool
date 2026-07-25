"""Tests for the AI register-map layer.

Covers: address normalization, PDF generation + text extraction + native-bytes,
the mock extraction pipeline, live validation against the simulator, and
profile save/load. No network / no real LLM required.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.ai import schema, pdf_source, extractor, profile_store, validation
from modbus_tool.ai.providers import MockProvider
from modbus_tool.modbus_client import ModbusService, TcpConfig
from modbus_tool.formatting import DisplayFormat

HOST, PORT, UNIT = "127.0.0.1", 5020, 1
failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    if not cond:
        failures.append(name)


def make_pdf(path):
    """A tiny register-map PDF whose registers map onto the simulator."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=letter)
    y = 720
    c.setFont("Helvetica", 11)
    rows = [
        "Register  Name            Type      Scale  Unit   Access",
        "40001     Ramp Value 0    uint16    1      -      R",
        "40011     Supply Temp     uint16    0.1    degC   R",
        "40101     Pi Constant     float32   1      bar    R",
        "40111     Energy Counter  uint32    1      kWh    R",
        "00001     Pump Run        bool      -      -      RW",
    ]
    for line in rows:
        c.drawString(50, y, line)
        y -= 20
    c.save()


def main():
    # ---- address normalization ------------------------------------ #
    addr, _ = schema.normalize_address("40001", "holding", "modicon")
    check("modicon 40001 -> 0", addr == 0, str(addr))
    addr, _ = schema.normalize_address("43665", "holding", "modicon")
    check("modicon 43665 -> 3664", addr == 3664, str(addr))
    addr, _ = schema.normalize_address("30001", "input", "modicon")
    check("modicon 30001 -> 0", addr == 0, str(addr))
    addr, _ = schema.normalize_address("100", "holding", "protocol")
    check("protocol 100 -> 100", addr == 100, str(addr))
    addr, _ = schema.normalize_address("1", "holding", "protocol1")
    check("protocol1 1 -> 0", addr == 0, str(addr))
    check("resolve float32", schema.resolve_format("float32") is DisplayFormat.FLOAT32)
    check("resolve uint32", schema.resolve_format("uint32") is DisplayFormat.U32)
    check("resolve bool->None", schema.resolve_format("bool") is None)

    # ---- PDF generation + ingestion ------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, "map.pdf")
        make_pdf(pdf_path)
        pages = pdf_source.extract_text(pdf_path)
        text = "\n".join(p.text for p in pages)
        check("pdf text has 40001", "40001" in text, "")
        check("pdf text has float32", "float32" in text, "")
        data = pdf_source.pdf_bytes_for_range(pdf_path, "")
        check("pdf bytes look like PDF", data[:4] == b"%PDF", str(data[:8]))

    # ---- mock extraction pipeline --------------------------------- #
    mock_regs = [
        {"printed_register": "40001", "table": "holding", "address_base": "modicon",
         "data_type": "uint16", "name": "Ramp Value 0", "access": "R"},
        {"printed_register": "40011", "table": "holding", "address_base": "modicon",
         "data_type": "uint16", "name": "Supply Temp", "scale": 0.1, "unit": "degC"},
        {"printed_register": "40101", "table": "holding", "address_base": "modicon",
         "data_type": "float32", "name": "Pi Constant", "unit": "bar"},
        {"printed_register": "40111", "table": "holding", "address_base": "modicon",
         "data_type": "uint32", "name": "Energy Counter", "unit": "kWh"},
        {"printed_register": "00001", "table": "coil", "address_base": "modicon",
         "data_type": "bool", "name": "Pump Run", "access": "RW"},
        # a register at addr 2 mis-typed as float -> should read but look implausible
        {"printed_register": "40003", "table": "holding", "address_base": "modicon",
         "data_type": "float32", "name": "Bad Guess"},
        # hallucinated out-of-range address -> should not read
        {"printed_register": "49999", "table": "holding", "address_base": "modicon",
         "data_type": "uint16", "name": "Ghost Register"},
    ]
    provider = MockProvider(mock_regs)
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, "map.pdf")
        make_pdf(pdf_path)
        profiles = extractor.extract_profiles(provider, pdf_path, on_log=lambda m: None)
    check("extracted 7 profiles", len(profiles) == 7, str(len(profiles)))

    by_name = {p.name: p for p in profiles}
    check("40011 normalized to addr 10", by_name["Supply Temp"].protocol_address == 10,
          str(by_name["Supply Temp"].protocol_address))

    # ---- live validation against the simulator -------------------- #
    svc = ModbusService()
    svc.connect_tcp(TcpConfig(host=HOST, port=PORT, timeout=2.0))
    summary = validation.validate_all(svc, profiles, UNIT)
    svc.close()

    check("Ramp Value 0 verified", by_name["Ramp Value 0"].status == "verified",
          by_name["Ramp Value 0"].live_value)
    check("Supply Temp scaled (0.1)", by_name["Supply Temp"].live_value.startswith("10"),
          by_name["Supply Temp"].live_value)  # raw 100 * 0.1 = 10
    check("Pi Constant ~3.14", by_name["Pi Constant"].live_value.startswith("3.14"),
          by_name["Pi Constant"].live_value)
    check("Energy Counter u32", by_name["Energy Counter"].live_value.startswith("74565"),
          by_name["Energy Counter"].live_value)
    check("Pump Run coil verified", by_name["Pump Run"].status == "verified",
          by_name["Pump Run"].live_value)
    check("Bad Guess flagged mismatch", by_name["Bad Guess"].status == "mismatch",
          by_name["Bad Guess"].live_value)
    check("Ghost Register unread", by_name["Ghost Register"].status == "unread",
          by_name["Ghost Register"].live_value)
    check("summary counts", summary["verified"] >= 5 and summary["unread"] == 1
          and summary["mismatch"] == 1, str(summary))

    # ---- profile save/load ---------------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        pth = os.path.join(tmp, "device.json")
        profile_store.save(pth, profiles, device_name="Test Device")
        name, loaded = profile_store.load(pth)
        check("save/load device name", name == "Test Device", name)
        check("save/load count", len(loaded) == len(profiles), str(len(loaded)))
        lb = {p.name: p for p in loaded}
        check("save/load renormalizes addr", lb["Supply Temp"].protocol_address == 10,
              str(lb["Supply Temp"].protocol_address))

    print()
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
