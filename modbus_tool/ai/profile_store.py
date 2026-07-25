"""Save/load a device profile (list of RegisterProfiles) as JSON."""

from __future__ import annotations

import json
from typing import List

from .schema import RegisterProfile, profile_from_dict


PROFILE_VERSION = 1


def to_dict(p: RegisterProfile) -> dict:
    """Serialize the document-derived fields (runtime fields are recomputed)."""
    return {
        "printed_register": p.printed_register,
        "table": p.table,
        "address_base": p.address_base,
        "name": p.name,
        "data_type": p.data_type,
        "word_order": p.word_order,
        "scale": p.scale,
        "offset": p.offset,
        "unit": p.unit,
        "access": p.access,
        "enums": {str(k): v for k, v in p.enums.items()},
        "description": p.description,
        "source_page": p.source_page,
        "source_snippet": p.source_snippet,
    }


def save(path: str, profiles: List[RegisterProfile], device_name: str = "") -> None:
    doc = {
        "version": PROFILE_VERSION,
        "device_name": device_name,
        "registers": [to_dict(p) for p in profiles],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def load(path: str) -> tuple[str, List[RegisterProfile]]:
    """Return (device_name, profiles). Profiles are normalized on load."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    device_name = doc.get("device_name", "")
    profiles = [profile_from_dict(d) for d in doc.get("registers", [])]
    return device_name, profiles
