"""Validate extracted profiles against a live device.

For each register the AI proposed, read it from the connected slave, decode it
with the proposed format/word-order, apply the proposed scaling, and judge
whether the result is plausible. This is the loop that catches hallucinated
addresses (they answer with an exception) and wrong formats/scaling (they decode
to absurd values). Reuses the same decoders as the manual tab.
"""

from __future__ import annotations

import math
from typing import Callable

from ..modbus_client import ModbusService, DataKind
from ..formatting import DisplayFormat, decode_registers, decode_bits, is_wide
from .schema import RegisterProfile, read_function_for


# numeric formats we can turn back into a float for scaling / sanity
_NUMERIC = {
    DisplayFormat.RAW_U16, DisplayFormat.S16,
    DisplayFormat.U32, DisplayFormat.S32, DisplayFormat.FLOAT32,
}


def _plausible_float(x: float) -> bool:
    if not math.isfinite(x):
        return False
    ax = abs(x)
    return x == 0.0 or (1e-6 <= ax <= 1e9)


def validate_profile(service: ModbusService, profile: RegisterProfile, unit: int) -> None:
    """Read one profile live and set profile.status / profile.live_value."""
    if profile.protocol_address is None:
        profile.status = "unread"
        profile.live_value = profile.norm_note or "no protocol address"
        return

    func = read_function_for(profile.table)

    if profile.kind is DataKind.BITS:
        r = service.read(func, profile.protocol_address, 1, unit)
        if not r.ok:
            profile.status = "unread"
            profile.live_value = r.error or ""
            return
        rows = decode_bits(r.values, profile.protocol_address)
        profile.status = "verified"
        profile.live_value = rows[0].value_text if rows else ""
        return

    # register read
    fmt = profile.resolved_format or DisplayFormat.RAW_U16
    count = 2 if is_wide(fmt) else 1
    r = service.read(func, profile.protocol_address, count, unit)
    if not r.ok:
        profile.status = "unread"
        profile.live_value = r.error or ""
        return

    rows = decode_registers(r.values, profile.protocol_address, fmt, profile.word_order)
    if not rows:
        profile.status = "unread"
        profile.live_value = "no data"
        return

    raw_text = rows[0].value_text
    if fmt in _NUMERIC:
        try:
            raw_val = float(raw_text)
        except ValueError:
            profile.status = "verified"
            profile.live_value = raw_text
            return
        eng = profile.apply_scaling(raw_val)
        unit_suffix = f" {profile.unit}" if profile.unit else ""
        profile.live_value = f"{eng:g}{unit_suffix}"
        # flag implausible float decodes (likely wrong format/word order)
        if fmt is DisplayFormat.FLOAT32 and not _plausible_float(raw_val):
            profile.status = "mismatch"
        else:
            profile.status = "verified"
    else:
        profile.status = "verified"
        profile.live_value = raw_text


def validate_all(
    service: ModbusService,
    profiles: list[RegisterProfile],
    unit: int,
    on_progress: Callable[[int, int], None] = lambda a, b: None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    """Validate every profile; return a summary count by status."""
    summary = {"verified": 0, "mismatch": 0, "unread": 0}
    total = len(profiles)
    for i, p in enumerate(profiles):
        if is_cancelled():
            break
        validate_profile(service, p, unit)
        summary[p.status] = summary.get(p.status, 0) + 1
        on_progress(i + 1, total)
    return summary
