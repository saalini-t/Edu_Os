"""Optional OCR. Nothing here is imported unless OCR is enabled, so ordinary text PDFs need no OCR dependencies.
Engines run locally (no data leaves the machine). `fake` is a deterministic test stand-in."""
from __future__ import annotations

import os
import threading
from typing import Protocol


class OcrUnavailable(Exception):
    pass


class OcrEngine(Protocol):
    name: str

    def ocr_page(self, image) -> str: ...      # image: numpy uint8 array, H x W x 3 (RGB)


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError as e:
                    raise OcrUnavailable("rapidocr-onnxruntime is not installed (pip install -r requirements-ocr.txt)") from e
                self._engine = RapidOCR()
        return self._engine

    def ocr_page(self, image) -> str:
        result, _ = self._load()(image)
        return "\n".join(r[1] for r in (result or []))


class FakeOcrEngine:
    """Test stand-in. EDUOS_FAKE_OCR_MODE: ok (default) | empty | fail_page_2 | crash | sleep."""
    name = "fake"
    _calls = 0

    def ocr_page(self, image) -> str:
        FakeOcrEngine._calls += 1
        mode = os.environ.get("EDUOS_FAKE_OCR_MODE", "ok")
        if mode == "empty":
            return ""
        if mode == "fail_page_2" and FakeOcrEngine._calls == 2:
            raise RuntimeError("simulated OCR failure")
        if mode == "crash":
            os._exit(139)           # hard crash of the (child) process
        if mode == "sleep":
            import time
            time.sleep(60)          # a hung parser: the parent's timeout must kill the child
        return (f"Scanned page text number {FakeOcrEngine._calls}. Routers forward packets between networks "
                f"using forwarding tables.")


def get_ocr_engine(name: str) -> OcrEngine | None:
    if name == "none":
        return None
    if name == "rapidocr":
        return RapidOcrEngine()
    if name == "fake":
        return FakeOcrEngine()
    raise ValueError(f"unknown OCR engine {name!r}")


def render_page(data: bytes, index: int, dpi: int, max_pixels: int):
    """Render one PDF page to an RGB numpy array, shrinking the scale so the bitmap stays under max_pixels."""
    import numpy as np
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(data)[index]
    w, h = page.get_size()
    scale = dpi / 72.0
    if w * h * scale * scale > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
    return np.array(page.render(scale=scale).to_pil().convert("RGB"))
