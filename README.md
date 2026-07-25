# ModbusTool

A cross-platform (Windows / Linux) **Modbus master** with a graphical interface —
a "Modbus Doctor"-style polling and diagnostics tool. Built with Python + PySide6
(Qt) and [pymodbus](https://pymodbus.readthedocs.io/).

**Iteration 1** delivered manual connection, reads/writes, live polling, and
per-read decoding. **Iteration 2** adds the **Discovery** tab: slave auto-discovery,
register-map discovery, and smart data-format detection. **Iteration 3** adds the
**Register Map (AI)** tab: extract a device's register map from its vendor PDF with
an LLM, then cross-check every register against the live device.

---

## Quick Start (first-time users)

### Get the app

**Windows — just download it (no Python needed):**
1. Open the [**Releases**](../../releases) page and download `ModbusTool.exe`.
2. Double-click it. Windows SmartScreen may warn that it's from an unknown
   publisher (the exe isn't code-signed) — click **More info → Run anyway**.
3. First launch takes a few seconds (it unpacks itself once).

**Any OS — run from source:**
```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
.venv/Scripts/python main.py                     # Linux/macOS: .venv/bin/python
```

### Read your first register (about 1 minute)

1. **Connection** panel (left): pick **TCP/IP** (enter the device IP + port 502)
   or **Serial (RTU)** (pick the COM port + baud). Click **Connect**.
2. On the **Manual** tab: set **Slave/Unit ID**, **Function** =
   *FC03 Read Holding Registers*, **Start address** = `0`, **Quantity** = `10`.
3. Click **Read Once** — values appear in the table. Pick a **Display format**
   (e.g. Float 32-bit) to decode them; use **Start Poll** for live updates.

> **No device to test with?** Run the built-in simulator (from source):
> `python tools/sim_slave.py`, then connect to `127.0.0.1` port `5020`, Unit ID `1`.

### The other two tabs

- **Discovery** — click *Scan Slaves* to find responding Unit IDs, then
  *Scan Registers* to map which addresses return data, with a suggested format.
- **Register Map (AI)** — load a vendor PDF and let an LLM extract the register
  map, then *Validate against device*. Needs either `ANTHROPIC_API_KEY` set in
  your environment (cloud) **or** LM Studio / Ollama running locally. Try it with
  the included `samples/sample_register_map.pdf` against the simulator.

New here and want the guided tour? See the sections below for each tab in detail.

---

## Features (v1) — Manual tab

- **Two transports, switchable in the UI**
  - **TCP/IP** — connect to a slave by IP address, port (default 502) and Unit ID.
    A read-only panel shows the PC's own IP / subnet / MAC for reference while
    debugging connectivity.
  - **Serial (RTU)** — pick COM port (auto-detected list), baud rate, data bits,
    parity, stop bits, timeout.
- **All common function codes**
  - Reads: FC01 Coils, FC02 Discrete Inputs, FC03 Holding Registers, FC04 Input Registers
  - Writes: FC05 / FC06 single, FC15 / FC16 multiple
- **Query builder** — Slave/Unit ID, start address (0-based *or* 1-based/40001-style),
  quantity up to **25** registers.
- **Display formats** (per read, applied live) — unsigned/signed 16-bit, hex, binary,
  unsigned/signed 32-bit, **float32**, hex32, with a selectable **byte/word order**
  (ABCD / CDAB / BADC / DCBA) for the 32-bit types.
- **Read once** or **continuous polling** at a configurable interval.
- **Activity log** of every operation and error (including device exception codes
  like *Illegal Data Address*).
- All Modbus I/O runs on a background thread, so the UI never freezes.

## Features (v2) — Discovery tab

1. **Slave discovery** — probe a Unit ID range and classify each address three ways:
   *responding* (green, returned data), *exception* (amber, answered with a Modbus
   error — present on a serial bus), *no-response* (grey, timeout). Gateway "target
   failed to respond" codes (10/11) are correctly treated as absent. Select a
   responding slave to load it into the register scan.
2. **Register discovery** — for a slave + function, walk an address range in blocks
   (≤25) and record which blocks return data vs. exceptions vs. silence, with a
   live value preview and a suggested format per block.
3. **Smart format detection** — select a responding block to see a ranked list of
   likely interpretations (16/32-bit, signed/unsigned, float32, byte/word order)
   with a confidence level, a decoded sample, and the reasoning. One click applies
   the chosen format to the Manual tab for live reading.

Scans run on the background thread with a live progress bar and a **Cancel** button.

## Features (v3) — Register Map (AI) tab

Solves the classic BMS pain: *"what is register 43665 for?"* Point it at the vendor's
Modbus PDF and get a structured, BACnet-like object list.

1. **Extract from PDF** — pick the register PDF, choose a backend, and the model
   returns each register's number, table, data type, scaling, unit, access, and
   enum/bitfield meanings, with the source page for each row.
   - **Anthropic (cloud)** — sends the *native PDF* to Claude (best table fidelity);
     reads `ANTHROPIC_API_KEY` from your environment (the app never handles the key).
   - **LM Studio (local)** — fully offline via LM Studio's OpenAI-compatible server
     (`http://localhost:1234/v1`); the same integration works with Ollama.
2. **Deterministic address math** — the *printed* Modicon number (`40001`, `43665`,
   `30001` …) is converted to a 0-based protocol address by the app, not the LLM,
   so there are no off-by-one hallucinations. Numbering base (Modicon / protocol /
   1-based) is per-register and editable.
3. **Human-in-the-loop review** — every field is editable in a table and re-normalizes
   on the fly; each row carries its source page.
4. **Live validation** — with the device connected, read every proposed register,
   decode + scale it, and flag each: 🟢 **verified** (plausible value), 🟠 **mismatch**
   (read OK but value implausible → wrong type/word-order/scale), ⚪ **unread** (no
   response → address likely wrong). This turns AI guesses into *verified* data.
5. **Device profiles** — save/load the map as JSON; send any register to the Manual
   tab to poll it live with its resolved format.

> The app **never handles your API key** — it reads `ANTHROPIC_API_KEY` from the
> environment (or use the local LM Studio backend to keep everything offline).

---

## Install & run

Dependencies are already installed in the bundled virtual environment (`.venv`).

**Windows:**
```bat
run.bat
```
or
```bat
.venv\Scripts\python.exe main.py
```

**Linux / macOS:**
```bash
./run.sh
```
or
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

> Requires Python 3.10+. PySide6 wheels are `abi3`, so they work on Python 3.14.

---

## Try it without hardware

A built-in Modbus TCP simulator serves recognizable test data.

```bash
# terminal 1 - start the simulator (127.0.0.1:5020, Unit ID 1)
.venv/Scripts/python.exe tools/sim_slave.py

# terminal 2 - launch the app, then in the UI:
#   Transport: TCP/IP   IP: 127.0.0.1   Port: 5020   Slave ID: 1
#   Function : FC03 Read Holding Registers   Address: 0   Quantity: 10
#   -> Read Once
.venv/Scripts/python.exe main.py
```

Simulator data map:
- Holding regs: `HR[i] = i*10`; `HR[100..101]` = float32 `3.14159`; `HR[110..111]` = u32 `0x00012345`
- Input regs: `IR[i] = 1000 + i`
- Coils: even addresses ON; Discrete inputs: every 3rd ON

---

## Tests

```bash
.venv/Scripts/python.exe tools/sim_slave.py          # start simulator first
.venv/Scripts/python.exe tools/smoke_test.py         # reads/writes/decoding + GUI build
.venv/Scripts/python.exe tools/poll_test.py          # threaded worker + polling
.venv/Scripts/python.exe tools/discovery_test.py     # slave/register scan + analyzer
.venv/Scripts/python.exe tools/gui_discovery_test.py # full GUI discovery workflow
.venv/Scripts/python.exe tools/ai_test.py            # PDF extract + normalize + live validate
.venv/Scripts/python.exe tools/gui_ai_test.py        # full GUI register-map workflow
```

The AI tests use a `MockProvider` (no network / no API key needed) and validate
extraction, Modicon normalization, and the live cross-check against the simulator.

---

## Project layout

```
ModbusTool/
├── main.py                    entry point
├── run.bat / run.sh           launchers
├── requirements.txt
├── modbus_tool/
│   ├── modbus_client.py       transport + function-code wrapper over pymodbus
│   ├── formatting.py          register -> display value decoding (4 byte orders)
│   ├── discovery.py           slave scan, register scan, format analyzer
│   ├── net_info.py            read-only PC network adapter info
│   ├── worker.py              background thread: connect/read/write/poll/scan/extract/validate
│   ├── main_window.py         PySide6 GUI (Manual + Discovery + Register Map tabs)
│   └── ai/                    register-map AI feature
│       ├── schema.py          RegisterProfile + deterministic Modicon address math
│       ├── pdf_source.py      PDF text extraction + native-bytes + page render
│       ├── providers.py       Mock / Anthropic / LM Studio (OpenAI-compatible) backends
│       ├── extractor.py       orchestrate extract -> normalize -> dedupe
│       ├── validation.py      live-read cross-check (verified/mismatch/unread)
│       └── profile_store.py   save/load device profiles as JSON
└── tools/
    ├── sim_slave.py           Modbus TCP simulator for testing
    ├── smoke_test.py          end-to-end read/write/decode checks
    ├── poll_test.py           threaded polling check
    ├── discovery_test.py      discovery-logic checks
    ├── gui_discovery_test.py  full GUI discovery workflow
    ├── ai_test.py             register-map extract/normalize/validate checks
    └── gui_ai_test.py         full GUI register-map workflow
```

---

## Roadmap

- [x] **v1** — manual connection, reads/writes, live polling, per-read decoding
- [x] **v2** — slave auto-discovery, register-map discovery, smart format detection
- [x] **v3** — AI PDF register-map extraction + live validation + device profiles

Ideas for later iterations:
- Feed discovery results into the AI extractor (validate a scanned map against a PDF).
- Vision-model path for fully scanned/image PDFs; per-vendor prompt tuning.
- Annotate the Manual/Discovery reads inline with names/units from a loaded profile.
- Export to CSV / BACnet EDE; scheduled logged polling to file.

---

## Build a standalone .exe (Windows)

```bat
build_exe.bat
```

Produces a single **`dist\ModbusTool.exe`** (~83 MB — it bundles Qt, the PDF
libraries with their native pdfium binary, and the AI SDKs). No Python install
needed to run it. First launch is a little slow (a one-file exe unpacks to a temp
dir); pass `--onedir` instead of `--onefile` in `build_exe.bat` for faster startup
at the cost of a folder instead of a single file.

> The AI libraries are imported lazily, so `build_exe.bat` force-includes them
> with `--collect-all`; don't drop those flags or Extract will fail in the exe.

Verify a build bundled everything (imports every dependency, incl. the lazy ones):

```bat
dist\ModbusTool.exe --selftest selftest.txt
```

Exit code 0 = all good; then read `selftest.txt` for the per-module report.

### Automated releases (GitHub Actions)

You don't have to build or upload the exe by hand. Pushing a version tag runs
`.github/workflows/build.yml`, which builds `ModbusTool.exe` on a Windows runner,
self-tests the bundle, and attaches it to a new GitHub Release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow also runs from the **Actions** tab (*Run workflow*) to produce the
exe as a downloadable build artifact without cutting a release.

## Notes

- On Linux, serial access may require adding your user to the `dialout` group.
- To package on Linux/macOS, run the same PyInstaller flags as `build_exe.bat`
  (the `--collect-all` list is platform-independent); the output is a native
  binary for that OS, not a `.exe`.

---

## Contributing

Issues and pull requests are welcome. To work on the code: fork the repo, create
a virtual environment (see Quick Start), and run the test suite before opening a
PR (`tools/*_test.py` — start `tools/sim_slave.py` first). Keep changes focused
and match the existing style.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
In short: you're free to use, study, share, and modify this software, but if you
distribute a modified version you must also release it under the GPLv3. It comes
with no warranty.

Copyright (C) 2026 Ibrahim Abdullatif.
