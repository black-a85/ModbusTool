"""Extraction orchestration: PDF -> provider -> normalized RegisterProfiles."""

from __future__ import annotations

from typing import Callable

from .providers import LLMProvider, PdfInput
from .schema import RegisterProfile, profile_from_dict


def extract_profiles(
    provider: LLMProvider,
    pdf_path: str,
    page_range: str = "",
    on_log: Callable[[str], None] = print,
) -> list[RegisterProfile]:
    """Run a provider over a PDF and return normalized, de-duplicated profiles."""
    pdf = PdfInput.load(pdf_path, page_range)
    on_log(f"Loaded {len(pdf.pages)} page(s) from PDF")

    raw = provider.extract(pdf, on_log=on_log)
    profiles = [profile_from_dict(d) for d in raw if isinstance(d, dict)]

    # de-dupe by (table, protocol_address); keep the first, richer entry wins
    seen: dict = {}
    ordered: list[RegisterProfile] = []
    for p in profiles:
        key = (p.table, p.protocol_address)
        if p.protocol_address is not None and key in seen:
            continue
        seen[key] = p
        ordered.append(p)

    ordered.sort(key=lambda p: (p.table, p.protocol_address if p.protocol_address is not None else 1 << 30))
    on_log(f"Normalized {len(ordered)} register(s)")
    return ordered
