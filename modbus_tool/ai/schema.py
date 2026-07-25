"""Register-map schema + deterministic address normalization.

The LLM reads the messy PDF and returns *what the document says* (the printed
register number, which table it belongs to, and which numbering convention the
document uses). This module does the arithmetic to turn that into a concrete
(function, 0-based protocol address) - deliberately kept OUT of the LLM, because
an off-by-one from a hallucinated conversion would read the wrong register.

Numbering conventions handled:
  * "modicon"   - Modicon 4xxxx/3xxxx/1xxxx/0xxxx (and 6-digit 4xxxxx...) numbers
  * "protocol"  - raw 0-based protocol address (what goes on the wire)
  * "protocol1" - 1-based protocol address (address - 1 on the wire)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..modbus_client import (
    ModbusFunction, DataKind,
    READ_HOLDING, READ_INPUT, READ_COILS, READ_DISCRETE,
)
from ..formatting import DisplayFormat


# canonical register tables
TABLES = ("holding", "input", "coil", "discrete")

_TABLE_TO_READ_FUNC = {
    "holding": READ_HOLDING,
    "input": READ_INPUT,
    "coil": READ_COILS,
    "discrete": READ_DISCRETE,
}


def read_function_for(table: str) -> ModbusFunction:
    return _TABLE_TO_READ_FUNC[table]


def table_kind(table: str) -> DataKind:
    return DataKind.BITS if table in ("coil", "discrete") else DataKind.REGISTERS


# --------------------------------------------------------------------------- #
# data-type text -> DisplayFormat
# --------------------------------------------------------------------------- #
_TYPE_ALIASES = {
    DisplayFormat.RAW_U16: {"uint16", "u16", "word", "unsigned", "unsigned16",
                            "ushort", "16bit", "uint", "register"},
    DisplayFormat.S16: {"int16", "s16", "short", "signed", "signed16", "int"},
    DisplayFormat.U32: {"uint32", "u32", "dword", "unsigned32", "ulong", "32bit"},
    DisplayFormat.S32: {"int32", "s32", "long", "signed32", "dint"},
    DisplayFormat.FLOAT32: {"float", "float32", "real", "ieee754", "ieee",
                            "single", "floatingpoint"},
    DisplayFormat.HEX16: {"hex", "hex16", "bitmask", "mask"},
}


def resolve_format(data_type: str) -> Optional[DisplayFormat]:
    """Map a free-text data-type string to a DisplayFormat (None = bit/boolean)."""
    if not data_type:
        return None
    key = data_type.lower().replace("-", "").replace("_", "").replace(" ", "")
    if key in ("bool", "boolean", "bit", "coil", "discrete", "digital"):
        return None
    for fmt, names in _TYPE_ALIASES.items():
        if key in names:
            return fmt
    # loose contains-based fallback
    if "float" in key or "real" in key:
        return DisplayFormat.FLOAT32
    if "32" in key:
        return DisplayFormat.S32 if key.startswith(("s", "i")) else DisplayFormat.U32
    if "int" in key and key.startswith(("s", "i")):
        return DisplayFormat.S16
    return DisplayFormat.RAW_U16


# --------------------------------------------------------------------------- #
# address normalization
# --------------------------------------------------------------------------- #
def _parse_int(printed: str) -> Optional[int]:
    s = str(printed).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except ValueError:
        # pull the first run of digits, if any
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else None


def _modicon_offset(table: str, n: int) -> int:
    six_digit = n >= 100000
    if table == "coil":
        return 1
    if table == "discrete":
        return 100001 if six_digit else 10001
    if table == "input":
        return 300001 if six_digit else 30001
    # holding
    return 400001 if six_digit else 40001


def normalize_address(printed: str, table: str, address_base: str) -> tuple[Optional[int], str]:
    """Return (protocol_address, note). protocol_address is None if it can't be
    resolved; note explains what happened / any warning."""
    n = _parse_int(printed)
    if n is None:
        return None, f"could not parse register '{printed}'"
    if table not in TABLES:
        return None, f"unknown table '{table}'"

    base = (address_base or "modicon").lower()
    if base == "protocol":
        return (n if n >= 0 else None), "raw protocol address"
    if base in ("protocol1", "protocol_1", "1based", "onebased"):
        return (n - 1 if n >= 1 else None), "1-based protocol address (-1)"

    # modicon
    offset = _modicon_offset(table, n)
    addr = n - offset
    if addr < 0:
        return None, (f"{printed} is below the {table} Modicon base ({offset}); "
                      f"numbering may be 'protocol' not 'modicon'")
    return addr, f"modicon {printed} -> protocol {addr}"


# --------------------------------------------------------------------------- #
# RegisterProfile
# --------------------------------------------------------------------------- #
@dataclass
class RegisterProfile:
    # what the document literally says
    printed_register: str
    table: str                       # holding | input | coil | discrete
    address_base: str = "modicon"    # modicon | protocol | protocol1
    name: str = ""
    data_type: str = ""              # free text from the doc
    word_order: str = "ABCD"
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    access: str = ""                 # 'R' | 'RW'
    enums: dict = field(default_factory=dict)   # {int: meaning}
    description: str = ""
    source_page: int = 0
    source_snippet: str = ""

    # filled in by normalization / validation (not from the LLM)
    protocol_address: Optional[int] = None
    resolved_format: Optional[DisplayFormat] = None
    norm_note: str = ""
    status: str = "unverified"       # unverified | verified | mismatch | unread
    live_value: str = ""             # decoded+scaled value from validation

    def normalize(self) -> None:
        """Fill protocol_address and resolved_format deterministically."""
        self.protocol_address, self.norm_note = normalize_address(
            self.printed_register, self.table, self.address_base)
        self.resolved_format = resolve_format(self.data_type)

    @property
    def kind(self) -> DataKind:
        return table_kind(self.table)

    def apply_scaling(self, raw_value: float):
        """raw -> engineering value using scale/offset."""
        return raw_value * self.scale + self.offset


# --------------------------------------------------------------------------- #
# JSON schema handed to the model for structured output
# --------------------------------------------------------------------------- #
REGISTER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "printed_register": {"type": "string",
                             "description": "The register number exactly as printed in the document, e.g. '43665' or '0x0E51'."},
        "table": {"type": "string", "enum": list(TABLES),
                  "description": "Which Modbus table: holding, input, coil, or discrete."},
        "address_base": {"type": "string", "enum": ["modicon", "protocol", "protocol1"],
                         "description": "Numbering convention the DOCUMENT uses. 'modicon' for 4xxxx/3xxxx style, 'protocol' for raw 0-based, 'protocol1' for 1-based."},
        "name": {"type": "string", "description": "Point name / label."},
        "data_type": {"type": "string",
                      "description": "Data type as stated, e.g. uint16, int16, uint32, float32, bool."},
        "word_order": {"type": "string", "enum": ["ABCD", "CDAB", "BADC", "DCBA"],
                       "description": "Byte/word order for 32-bit types; default ABCD if unspecified."},
        "scale": {"type": "number", "description": "Multiplier to apply to the raw value (gain). 1 if none."},
        "offset": {"type": "number", "description": "Value added after scaling. 0 if none."},
        "unit": {"type": "string", "description": "Engineering unit, e.g. degC, kWh, %, Pa. Empty if none."},
        "access": {"type": "string", "description": "'R' for read-only or 'RW' if writable."},
        "enums": {"type": "object",
                  "description": "Coded value meanings as a map of number->text, e.g. {\"0\":\"Off\",\"1\":\"Auto\"}. Empty if not applicable.",
                  "additionalProperties": {"type": "string"}},
        "description": {"type": "string", "description": "Any extra notes."},
        "source_page": {"type": "integer", "description": "1-based PDF page number this row came from."},
        "source_snippet": {"type": "string", "description": "Short verbatim snippet from the page supporting this row."},
    },
    "required": ["printed_register", "table", "data_type"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "registers": {"type": "array", "items": REGISTER_ITEM_SCHEMA},
    },
    "required": ["registers"],
}


def profile_from_dict(d: dict) -> RegisterProfile:
    """Build and normalize a RegisterProfile from a raw model/JSON dict."""
    enums_raw = d.get("enums") or {}
    enums = {}
    for k, v in enums_raw.items():
        try:
            enums[int(k)] = str(v)
        except (ValueError, TypeError):
            continue
    p = RegisterProfile(
        printed_register=str(d.get("printed_register", "")).strip(),
        table=str(d.get("table", "holding")).strip().lower(),
        address_base=str(d.get("address_base", "modicon")).strip().lower(),
        name=str(d.get("name", "")).strip(),
        data_type=str(d.get("data_type", "")).strip(),
        word_order=str(d.get("word_order", "ABCD")).strip().upper() or "ABCD",
        scale=_as_float(d.get("scale"), 1.0),
        offset=_as_float(d.get("offset"), 0.0),
        unit=str(d.get("unit", "")).strip(),
        access=str(d.get("access", "")).strip().upper(),
        enums=enums,
        description=str(d.get("description", "")).strip(),
        source_page=int(d.get("source_page", 0) or 0),
        source_snippet=str(d.get("source_snippet", "")).strip(),
    )
    p.normalize()
    return p


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default
