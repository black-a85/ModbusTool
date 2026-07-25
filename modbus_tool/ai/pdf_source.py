"""PDF ingestion for register-map extraction.

Text-first (most Modbus PDFs have selectable text): pull text per page with
pdfplumber. For pages that are essentially images (scanned docs), render them to
PNG with pypdfium2 so a vision-capable model can read them.

Both libraries are permissively licensed (pdfminer.six = MIT/BSD, pypdfium2 =
BSD/Apache), unlike AGPL PyMuPDF.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass


@dataclass
class PageContent:
    number: int          # 1-based page number
    text: str            # extracted text ("" if none)
    has_text: bool       # True if the page has usable selectable text
    image_png_b64: str = ""   # populated only when rendered for vision


# a page with fewer than this many text characters is treated as image-only
_MIN_TEXT_CHARS = 25


def page_count(path: str) -> int:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


def _parse_range(page_range: str, total: int) -> list[int]:
    """Parse '1-5,8,11-13' (1-based) into a sorted unique 0-based index list.
    Empty/None means all pages."""
    if not page_range or not page_range.strip():
        return list(range(total))
    out: set[int] = set()
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            for p in range(start, end + 1):
                if 1 <= p <= total:
                    out.add(p - 1)
        else:
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= total:
                out.add(p - 1)
    return sorted(out)


def extract_text(path: str, page_range: str = "") -> list[PageContent]:
    """Extract text for the given page range."""
    import pdfplumber
    pages: list[PageContent] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for idx in _parse_range(page_range, total):
            page = pdf.pages[idx]
            text = page.extract_text() or ""
            pages.append(PageContent(
                number=idx + 1,
                text=text,
                has_text=len(text.strip()) >= _MIN_TEXT_CHARS,
            ))
    return pages


def render_page_png(path: str, page_index0: int, scale: float = 2.0) -> str:
    """Render a single (0-based) page to a base64 PNG for vision models."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    try:
        page = pdf[page_index0]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        pdf.close()


def pdf_bytes_for_range(path: str, page_range: str = "") -> bytes:
    """Return PDF bytes containing only the pages in page_range (whole file if
    empty). Used to hand a native PDF to a vision-capable model (e.g. Claude
    reads tables far better from the real PDF than from extracted text)."""
    import pypdf
    reader = pypdf.PdfReader(path)
    total = len(reader.pages)
    indices = _parse_range(page_range, total)
    if not indices or len(indices) == total:
        with open(path, "rb") as f:
            return f.read()
    writer = pypdf.PdfWriter()
    for idx in indices:
        writer.add_page(reader.pages[idx])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def load_pages(path: str, page_range: str = "", want_images: bool = True) -> list[PageContent]:
    """Text for every page in range; for image-only pages, also attach a PNG
    render (when want_images) so a vision model can be used."""
    pages = extract_text(path, page_range)
    if want_images:
        for pc in pages:
            if not pc.has_text:
                try:
                    pc.image_png_b64 = render_page_png(path, pc.number - 1)
                except Exception:
                    pc.image_png_b64 = ""
    return pages
