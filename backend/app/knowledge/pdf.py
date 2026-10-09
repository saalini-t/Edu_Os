"""PDF text extraction with optional per-page OCR. This module must stay free of web/database imports: it is also the
body of the sandboxed child process (app.knowledge.parse_child)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field

from pypdf import PdfReader
from pypdf.errors import PyPdfError


class PdfError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PageResult:
    page: int                 # 1-based
    text: str
    method: str               # text | ocr | empty | failed | ocr_budget_exceeded
    chars: int
    error: str | None = None


@dataclass
class Extraction:
    pages: list[PageResult]
    ocr_engine: str | None = None

    def texts(self) -> list[str]:
        return [p.text for p in self.pages]

    def report(self) -> dict:
        by = lambda m: [p.page for p in self.pages if p.method == m]    # noqa: E731
        return {"ocr_engine": self.ocr_engine, "ocr_pages": by("ocr"), "empty_pages": by("empty"),
                "failed_pages": [{"page": p.page, "error": p.error} for p in self.pages if p.method == "failed"],
                "ocr_budget_exceeded_pages": by("ocr_budget_exceeded"),
                "pages": [{"page": p.page, "method": p.method, "chars": p.chars} for p in self.pages]}


def _chars(s: str) -> int:
    return len("".join(s.split()))


def extract_document(data: bytes, *, max_pages: int, ocr=None, min_chars: int = 20, max_ocr_pages: int = 20,
                     dpi: int = 150, max_pixels: int = 6_000_000) -> Extraction:
    """Text layer first; OCR ONLY for pages whose text layer has fewer than `min_chars` non-space characters.
    Page-level failures are recorded and do not abort the document; the document fails only if nothing usable remains."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PdfError("ENCRYPTED_PDF", "Encrypted PDFs are not supported")
        n = len(reader.pages)
        if n > max_pages:
            raise PdfError("TOO_MANY_PAGES", f"PDF has more than {max_pages} pages")
        pages: list[PageResult] = []
        ocr_used = 0
        for i in range(n):
            err: str | None = None
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception as e:      # a damaged page must not kill the document
                text, err = "", f"text:{type(e).__name__}"
            c = _chars(text)
            if c >= min_chars or (c > 0 and ocr is None):
                pages.append(PageResult(i + 1, text, "text", c))
                continue
            if ocr is None:
                pages.append(PageResult(i + 1, text, "failed" if err else "empty", c, err))
                continue
            if ocr_used >= max_ocr_pages:
                pages.append(PageResult(i + 1, text, "ocr_budget_exceeded", c, f"OCR page budget ({max_ocr_pages}) used up"))
                continue
            ocr_used += 1
            try:
                from app.knowledge.ocr import render_page
                ocr_text = ocr.ocr_page(render_page(data, i, dpi, max_pixels)) or ""
                oc = _chars(ocr_text)
                pages.append(PageResult(i + 1, ocr_text if oc else text, "ocr" if oc else "empty", max(oc, c)))
            except Exception as e:
                pages.append(PageResult(i + 1, text, "failed", c, f"ocr:{type(e).__name__}"))
    except PdfError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError) as e:
        raise PdfError("UNREADABLE_PDF", f"Could not parse PDF ({type(e).__name__})")
    if not any(p.chars > 0 for p in pages):
        hint = ("; scanned pages need OCR (set OCR_ENGINE)" if ocr is None else
                "; OCR found no text on the image-only pages")
        raise PdfError("NO_EXTRACTABLE_TEXT", "PDF contains no extractable text" + hint)
    return Extraction(pages, getattr(ocr, "name", None))


def extract_pages(data: bytes, max_pages: int) -> list[str]:
    """Text-layer-only extraction (kept for evaluation scripts and tests)."""
    return extract_document(data, max_pages=max_pages).texts()


# ------------------------------------------------------------------------------------------ sandboxed parsing
def parse_document(data: bytes, settings) -> Extraction:
    """Parse an untrusted PDF according to settings: inline, or in a child process with a timeout (and, on POSIX, an
    address-space limit) so a malicious or pathological file cannot take the worker/API down."""
    params = {"max_pages": settings.max_pdf_pages, "ocr_engine": settings.ocr_engine,
              "min_chars": settings.ocr_min_text_chars, "max_ocr_pages": settings.ocr_max_pages,
              "dpi": settings.ocr_dpi, "max_pixels": settings.ocr_max_pixels, "memory_mb": settings.parser_memory_mb}
    if settings.parser_isolation == "inline":
        from app.knowledge.ocr import get_ocr_engine
        return extract_document(data, max_pages=params["max_pages"], ocr=get_ocr_engine(params["ocr_engine"]),
                                min_chars=params["min_chars"], max_ocr_pages=params["max_ocr_pages"],
                                dpi=params["dpi"], max_pixels=params["max_pixels"])
    try:
        proc = subprocess.run([sys.executable, "-m", "app.knowledge.parse_child", json.dumps(params)], input=data,
                              capture_output=True, timeout=settings.parser_timeout_s)
    except subprocess.TimeoutExpired:
        raise PdfError("PARSER_TIMEOUT", f"parsing exceeded {settings.parser_timeout_s:.0f}s and was killed")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if "MemoryError" in err:
            raise PdfError("PARSER_RESOURCE_LIMIT", "parser exceeded its memory limit")
        raise PdfError("PARSER_CRASHED", f"parser process exited with status {proc.returncode}")
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        raise PdfError("PARSER_CRASHED", "parser produced invalid output")
    if not out.get("ok"):
        raise PdfError(out.get("code", "UNREADABLE_PDF"), out.get("message", "parse failed"))
    return Extraction([PageResult(**p) for p in out["pages"]], out.get("ocr_engine"))
