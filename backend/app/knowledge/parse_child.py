"""Sandboxed PDF parser. Reads params (JSON, argv[1]) and the PDF bytes (stdin); prints one JSON document to stdout.
Imports nothing from the web/database layers, so a hostile PDF can only affect this short-lived process."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict


def _limit_memory(mb: int) -> None:
    if mb <= 0:
        return
    try:
        import resource                       # POSIX only
        limit = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:                         # Windows: no rlimit; the parent's timeout still applies
        pass


def main() -> int:
    params = json.loads(sys.argv[1])
    _limit_memory(int(params.get("memory_mb", 0)))
    data = sys.stdin.buffer.read()
    from app.knowledge.ocr import get_ocr_engine
    from app.knowledge.pdf import PdfError, extract_document
    try:
        ex = extract_document(data, max_pages=params["max_pages"], ocr=get_ocr_engine(params["ocr_engine"]),
                              min_chars=params["min_chars"], max_ocr_pages=params["max_ocr_pages"],
                              dpi=params["dpi"], max_pixels=params["max_pixels"])
        out = {"ok": True, "ocr_engine": ex.ocr_engine, "pages": [asdict(p) for p in ex.pages]}
    except PdfError as e:
        out = {"ok": False, "code": e.code, "message": str(e)}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
