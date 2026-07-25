"""LLM providers for register-map extraction.

A provider takes a PDF (as native bytes and/or extracted page text) plus an
instruction, calls a model, and returns a list of raw register dicts. Normalizing
and validating those dicts is the extractor's job, not the provider's.

Backends:
  * MockProvider          - deterministic canned output, for tests (no network).
  * AnthropicProvider      - Claude via the official `anthropic` SDK. Sends the
                             native PDF (best table fidelity) and forces a
                             tool call for structured output.
  * OpenAICompatProvider   - LM Studio / Ollama via their OpenAI-compatible API
                             (the `openai` SDK pointed at localhost). Sends
                             extracted page text and requests JSON.

SDK imports are lazy so the app runs even if a given SDK isn't installed; a
missing SDK only errors when that provider is actually used.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from .schema import EXTRACTION_SCHEMA
from . import pdf_source


DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

SYSTEM_INSTRUCTION = (
    "You extract Modbus register maps from device documentation. "
    "Return one entry per register/point you find in the document's register "
    "tables. Copy the register number exactly as printed. Identify which table "
    "it belongs to (holding/input/coil/discrete) and which numbering convention "
    "the document uses (modicon for 4xxxx/3xxxx style, protocol for raw 0-based, "
    "protocol1 for 1-based). Include data type, scaling (gain/offset), unit, "
    "read/write access, and any enum/bitfield meanings when the document states "
    "them. For each entry, record the 1-based source page and a short verbatim "
    "snippet. Do not invent registers that are not in the document."
)


@dataclass
class PdfInput:
    """Everything a provider might need about the PDF being extracted."""
    path: str
    page_range: str = ""
    pages: list = field(default_factory=list)   # list[pdf_source.PageContent]
    pdf_bytes: bytes = b""

    @classmethod
    def load(cls, path: str, page_range: str = "") -> "PdfInput":
        pages = pdf_source.load_pages(path, page_range, want_images=False)
        try:
            data = pdf_source.pdf_bytes_for_range(path, page_range)
        except Exception:
            data = b""
        return cls(path=path, page_range=page_range, pages=pages, pdf_bytes=data)


class LLMProvider:
    """Interface. extract() returns a list of raw register dicts."""

    name = "base"

    def extract(self, pdf: PdfInput, on_log: Callable[[str], None] = print) -> list[dict]:
        raise NotImplementedError

    def check(self) -> tuple[bool, str]:
        """Return (ok, detail) for a lightweight connectivity/config check."""
        return True, ""


# --------------------------------------------------------------------------- #
# Mock (tests)
# --------------------------------------------------------------------------- #
class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, registers: list[dict]):
        self._registers = registers

    def extract(self, pdf: PdfInput, on_log: Callable[[str], None] = print) -> list[dict]:
        on_log(f"[mock] returning {len(self._registers)} register(s)")
        return list(self._registers)


# --------------------------------------------------------------------------- #
# Anthropic (Claude)
# --------------------------------------------------------------------------- #
class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: Optional[str] = None):
        self.model = model
        self._api_key = api_key  # None -> SDK resolves from env / profile

    def _client(self):
        import anthropic
        if self._api_key:
            return anthropic.Anthropic(api_key=self._api_key)
        return anthropic.Anthropic()

    def check(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"anthropic SDK not installed: {exc}"
        return True, f"model {self.model}"

    def extract(self, pdf: PdfInput, on_log: Callable[[str], None] = print) -> list[dict]:
        client = self._client()

        # Force structured output via a tool call (no additionalProperties:false
        # constraint, unlike strict structured outputs).
        tool = {
            "name": "record_registers",
            "description": "Record the register map extracted from the document.",
            "input_schema": EXTRACTION_SCHEMA,
        }

        content: list = []
        if pdf.pdf_bytes:
            on_log("[anthropic] sending native PDF document")
            b64 = base64.standard_b64encode(pdf.pdf_bytes).decode("ascii")
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            })
        else:
            on_log("[anthropic] sending extracted page text")
            text = "\n\n".join(f"--- Page {p.number} ---\n{p.text}" for p in pdf.pages)
            content.append({"type": "text", "text": text})
        content.append({"type": "text", "text":
                        "Extract every register in this document into the "
                        "record_registers tool."})

        on_log(f"[anthropic] calling {self.model} ...")
        resp = client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_INSTRUCTION,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_registers"},
            messages=[{"role": "user", "content": content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_registers":
                regs = block.input.get("registers", [])
                on_log(f"[anthropic] extracted {len(regs)} register(s)")
                return list(regs)
        on_log("[anthropic] model returned no tool call")
        return []


# --------------------------------------------------------------------------- #
# OpenAI-compatible (LM Studio, Ollama)
# --------------------------------------------------------------------------- #
class OpenAICompatProvider(LLMProvider):
    name = "openai-compat"

    def __init__(self, base_url: str, model: str, api_key: str = "not-needed"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key or "not-needed"

    def _client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self._api_key)

    def check(self) -> tuple[bool, str]:
        try:
            from openai import OpenAI  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"openai SDK not installed: {exc}"
        try:
            client = self._client()
            models = client.models.list()
            ids = [m.id for m in models.data][:5]
            return True, f"reachable at {self.base_url}; models: {', '.join(ids) or 'none'}"
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot reach {self.base_url}: {exc}"

    def extract(self, pdf: PdfInput, on_log: Callable[[str], None] = print) -> list[dict]:
        client = self._client()
        text = "\n\n".join(f"--- Page {p.number} ---\n{p.text}" for p in pdf.pages)

        schema_hint = json.dumps(EXTRACTION_SCHEMA, indent=2)
        user = (
            "Document text follows. Extract every Modbus register into JSON.\n\n"
            f"Return a JSON object matching this schema:\n{schema_hint}\n\n"
            "Respond with ONLY the JSON object, no prose.\n\n"
            f"=== DOCUMENT ===\n{text}"
        )

        on_log(f"[lmstudio] calling {self.model} at {self.base_url} ...")
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
        # try JSON mode; fall back if the server rejects response_format
        try:
            resp = client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs)
        except Exception:
            resp = client.chat.completions.create(**kwargs)

        raw = resp.choices[0].message.content or ""
        data = _loads_lenient(raw)
        regs = data.get("registers", []) if isinstance(data, dict) else []
        on_log(f"[lmstudio] extracted {len(regs)} register(s)")
        return list(regs)


def _loads_lenient(text: str):
    """Parse JSON, tolerating leading/trailing prose or code fences."""
    text = text.strip()
    if text.startswith("```"):
        # strip a ```json ... ``` fence
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return {}
        return {}
