"""Generate a realistic sample Modbus register-map PDF whose registers map onto
tools/sim_slave.py, so you can test the whole AI pipeline (extract -> validate)
locally with no hardware.

Run:  python samples/make_sample_pdf.py
Produces: samples/sample_register_map.pdf
"""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)


HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sample_register_map.pdf")


def build():
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(OUT, pagesize=A4,
                            topMargin=20 * mm, bottomMargin=20 * mm)
    story = []
    story.append(Paragraph("ACME FlowMaster 3000 - Modbus Register Map", styles["Title"]))
    story.append(Paragraph("Document rev 1.2", styles["Normal"]))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        "Register addresses use the Modicon convention: 4xxxx = holding "
        "registers, 3xxxx = input registers, 0xxxx = coils. 32-bit values are "
        "big-endian (high word first). Slave/Unit ID 1.", styles["Normal"]))
    story.append(Spacer(1, 6 * mm))

    header = ["Register", "Description", "Type", "Scale", "Unit", "Access", "Notes"]
    rows = [
        ["40001", "Ramp Counter", "UINT16", "1", "-", "R", "Test ramp, increments by 10"],
        ["40011", "Supply Temperature", "UINT16", "0.1", "°C", "R", "e.g. 100 = 10.0 C"],
        ["40101", "System Pressure", "FLOAT32", "1", "bar", "R", "IEEE754, 2 registers"],
        ["40111", "Energy Total", "UINT32", "1", "kWh", "R", "2 registers, hi word first"],
        ["30001", "Raw Sensor Input", "UINT16", "1", "counts", "R", "Input register"],
        ["00001", "Pump Command", "BOOL", "-", "-", "R/W", "0 = Off, 1 = On"],
    ]

    table = Table([header] + rows, repeatRows=1,
                  colWidths=[22 * mm, 40 * mm, 20 * mm, 14 * mm, 16 * mm, 16 * mm, 44 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "Pump Command enumeration: 0 = Off, 1 = On. All holding registers are "
        "read/write unless marked R.", styles["Normal"]))

    doc.build(story)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
