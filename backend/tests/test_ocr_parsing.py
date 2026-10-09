"""OCR is optional and page-level; untrusted PDFs are parsed in a sandboxed child process."""
import io
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import make_session_factory
from app.knowledge.ocr import FakeOcrEngine, get_ocr_engine
from app.knowledge.pdf import PdfError, extract_document, parse_document
from app.main import create_app
from app.models import Chunk
from app.worker import run_once
from tests.conftest import login, make_pdf

TEXT_PAGE = "Routers forward packets between networks using forwarding tables and longest prefix match."


def _scan_image(text_lines):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1240, 160 * len(text_lines) + 80), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arial.ttf", 44)
    except Exception:
        try:
            f = ImageFont.truetype("DejaVuSans.ttf", 44)
        except Exception:
            f = ImageFont.load_default()
    for i, line in enumerate(text_lines):
        d.text((40, 50 + 150 * i), line, fill="black", font=f)
    return img


def scanned_pdf(pages, text_first=False):
    """pages: list of lists of text lines rendered as an IMAGE (no text layer). text_first adds a real text page 1."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    if text_first:
        c.drawString(72, 750, TEXT_PAGE)
        c.showPage()
    for lines in pages:
        img = _scan_image(lines)
        c.drawImage(ImageReader(img), 30, 400, width=540, height=540 * img.height / img.width)
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture(autouse=True)
def reset_fake(monkeypatch):
    FakeOcrEngine._calls = 0
    monkeypatch.delenv("EDUOS_FAKE_OCR_MODE", raising=False)


# ----------------------------------------------------------------------------------------- page-level extraction
def test_text_pdfs_never_invoke_ocr():
    ocr = FakeOcrEngine()
    ex = extract_document(make_pdf([TEXT_PAGE]), max_pages=10, ocr=ocr)
    assert FakeOcrEngine._calls == 0 and [p.method for p in ex.pages] == ["text"]
    assert ex.report()["ocr_pages"] == []


def test_ocr_runs_only_on_pages_without_a_text_layer():
    ex = extract_document(scanned_pdf([["scanned page"]], text_first=True), max_pages=10, ocr=FakeOcrEngine())
    assert [p.method for p in ex.pages] == ["text", "ocr"] and FakeOcrEngine._calls == 1
    assert ex.pages[0].text.startswith("Routers") and "Scanned page text" in ex.pages[1].text
    rep = ex.report()
    assert rep["ocr_pages"] == [2] and rep["ocr_engine"] == "fake" and rep["failed_pages"] == []


def test_image_only_pdf_without_ocr_fails_clearly_and_mentions_ocr():
    with pytest.raises(PdfError) as e:
        extract_document(scanned_pdf([["scan"]]), max_pages=10, ocr=None)
    assert e.value.code == "NO_EXTRACTABLE_TEXT" and "OCR_ENGINE" in str(e.value)


def test_ocr_that_finds_nothing_is_reported(monkeypatch):
    monkeypatch.setenv("EDUOS_FAKE_OCR_MODE", "empty")
    with pytest.raises(PdfError) as e:
        extract_document(scanned_pdf([["scan"]]), max_pages=10, ocr=FakeOcrEngine())
    assert e.value.code == "NO_EXTRACTABLE_TEXT" and "OCR found no text" in str(e.value)


def test_ocr_page_budget_is_enforced_and_reported():
    ex = extract_document(scanned_pdf([["a"], ["b"], ["c"]], text_first=True), max_pages=10, ocr=FakeOcrEngine(),
                          max_ocr_pages=1)
    assert FakeOcrEngine._calls == 1
    rep = ex.report()
    assert rep["ocr_pages"] == [2] and rep["ocr_budget_exceeded_pages"] == [3, 4]


def test_a_failing_ocr_page_is_recorded_but_does_not_fail_the_document(monkeypatch):
    monkeypatch.setenv("EDUOS_FAKE_OCR_MODE", "fail_page_2")
    ex = extract_document(scanned_pdf([["a"], ["b"], ["c"]]), max_pages=10, ocr=FakeOcrEngine())
    rep = ex.report()
    assert [p.method for p in ex.pages] == ["ocr", "failed", "ocr"]
    assert rep["failed_pages"] == [{"page": 2, "error": "ocr:RuntimeError"}] and rep["ocr_pages"] == [1, 3]


def test_render_scale_respects_the_pixel_cap():
    from app.knowledge.ocr import render_page
    data = scanned_pdf([["x"]])
    small = render_page(data, 0, dpi=300, max_pixels=200_000)
    assert small.shape[0] * small.shape[1] <= 200_000 * 1.05 and small.shape[2] == 3


def test_unknown_ocr_engine_is_rejected():
    assert get_ocr_engine("none") is None
    with pytest.raises(ValueError):
        get_ocr_engine("bogus")


# ----------------------------------------------------------------------------------------- sandboxed parsing
def _s(settings, **kw):
    return settings.model_copy(update=kw)


def test_subprocess_parsing_matches_inline_parsing(settings):
    data = make_pdf([TEXT_PAGE, "Second page about switches and ARP resolution."])
    a = parse_document(data, _s(settings, parser_isolation="inline"))
    b = parse_document(data, _s(settings, parser_isolation="subprocess"))
    assert a.texts() == b.texts() and a.report() == b.report()


def test_subprocess_reports_structured_errors(settings):
    s = _s(settings, parser_isolation="subprocess")
    with pytest.raises(PdfError) as e:
        parse_document(b"%PDF-1.4\nnot really a pdf", s)
    assert e.value.code == "UNREADABLE_PDF"
    with pytest.raises(PdfError) as e:
        parse_document(make_pdf(["a"] * 1) , _s(s, max_pdf_pages=0))
    assert e.value.code == "TOO_MANY_PAGES"


def test_a_hung_parser_is_killed_by_the_timeout(settings, monkeypatch):
    monkeypatch.setenv("EDUOS_FAKE_OCR_MODE", "sleep")
    s = _s(settings, parser_isolation="subprocess", ocr_engine="fake", parser_timeout_s=3.0)
    t0 = time.time()
    with pytest.raises(PdfError) as e:
        parse_document(scanned_pdf([["scan"]]), s)
    assert e.value.code == "PARSER_TIMEOUT" and time.time() - t0 < 20


def test_a_crashing_parser_cannot_take_down_the_caller(settings, monkeypatch):
    monkeypatch.setenv("EDUOS_FAKE_OCR_MODE", "crash")
    s = _s(settings, parser_isolation="subprocess", ocr_engine="fake")
    with pytest.raises(PdfError) as e:
        parse_document(scanned_pdf([["scan"]]), s)
    assert e.value.code == "PARSER_CRASHED"


@pytest.mark.skipif(os.name == "nt", reason="RLIMIT_AS is POSIX-only (verified separately in Docker)")
def test_memory_limit_kills_an_oversized_parse(settings):
    s = _s(settings, parser_isolation="subprocess", parser_memory_mb=32)
    with pytest.raises(PdfError) as e:
        parse_document(make_pdf([TEXT_PAGE]), s)
    assert e.value.code in ("PARSER_RESOURCE_LIMIT", "PARSER_CRASHED")


# ----------------------------------------------------------------------------------------- through the ingestion pipeline
@pytest.fixture
def pipe(settings, database, cn_course_id):
    s = _s(settings, ingestion_mode="async", redis_url=None, parser_isolation="inline", job_retry_backoff_seconds=0.0)
    c = TestClient(create_app(s), raise_server_exceptions=False)
    return type("P", (), {"c": c, "h": login(c, "student1@demo.local"), "cid": cn_course_id, "s": s,
                          "f": make_session_factory(database)})


def upload(p, data, title="Scan"):
    return p.c.post("/v1/documents", headers=p.h, data={"course_id": p.cid, "title": title},
                    files={"file": ("scan.pdf", io.BytesIO(data), "application/pdf")}).json()


def test_a_crashing_parser_fails_the_job_but_not_the_worker(pipe, monkeypatch):
    monkeypatch.setenv("EDUOS_FAKE_OCR_MODE", "crash")
    s = _s(pipe.s, parser_isolation="subprocess", ocr_engine="fake")
    d = upload(pipe, scanned_pdf([["scan"]]))
    assert run_once(pipe.f, s, "w") == 1                              # the worker survives and reports
    st = pipe.c.get(f"/v1/documents/{d['document_id']}", headers=pipe.h).json()
    assert st["status"] == "FAILED" and st["error_code"] == "PARSER_CRASHED" and st["job"]["attempts"] == 1


def test_reindex_after_enabling_ocr_recovers_scanned_pages(pipe):
    """A mixed document is first indexed WITHOUT OCR (scanned page skipped and reported), then re-indexed with OCR."""
    d = upload(pipe, scanned_pdf([["scan"]], text_first=True))
    run_once(pipe.f, pipe.s, "w")
    st = pipe.c.get(f"/v1/documents/{d['document_id']}", headers=pipe.h).json()
    assert st["status"] == "READY" and st["extraction"]["empty_pages"] == [2] and st["extraction"]["ocr_pages"] == []
    def hits(q):
        return pipe.c.get("/v1/search", headers=pipe.h, params={"q": q, "course_id": pipe.cid}).json()["results"]
    assert any("Routers forward packets" in h["text"] for h in hits("routers forward packets forwarding tables"))
    assert not any("Scanned page text" in h["text"] for h in hits("scanned page text"))    # page 2 was skipped, not invented
    ocr_on = _s(pipe.s, ocr_engine="fake")
    assert pipe.c.post(f"/v1/documents/{d['document_id']}/reindex", headers=pipe.h).status_code == 202
    run_once(pipe.f, ocr_on, "w")
    st = pipe.c.get(f"/v1/documents/{d['document_id']}", headers=pipe.h).json()
    assert st["version"] == 2 and st["extraction"]["ocr_pages"] == [2] and st["extraction"]["empty_pages"] == []
    assert any("Scanned page text" in h["text"] for h in hits("scanned page text"))
    with pipe.f() as db:
        pages = {c.page for c in db.scalars(select(Chunk).where(Chunk.document_id == uuid.UUID(d["document_id"])))}
        assert pages == {1, 2} and all(c.ingestion_version == 2 for c in
                                       db.scalars(select(Chunk).where(Chunk.document_id == uuid.UUID(d["document_id"]))))


def test_real_rapidocr_end_to_end(pipe, settings):
    pytest.importorskip("rapidocr_onnxruntime")
    pytest.importorskip("pypdfium2")
    s = _s(pipe.s, ocr_engine="rapidocr", parser_isolation="subprocess", parser_timeout_s=180.0)
    data = scanned_pdf([["The sliding window protocol limits unacknowledged data.",
                         "Congestion control prevents router queues overflowing."]])
    d = upload(pipe, data)
    run_once(pipe.f, s, "w")
    st = pipe.c.get(f"/v1/documents/{d['document_id']}", headers=pipe.h).json()
    assert st["status"] == "READY", st
    assert st["extraction"]["ocr_engine"] == "rapidocr" and st["extraction"]["ocr_pages"] == [1]
    hits = pipe.c.get("/v1/search", headers=pipe.h, params={"q": "sliding window protocol unacknowledged", "course_id": pipe.cid}).json()["results"]
    assert any("sliding window" in h["text"].lower() for h in hits)
