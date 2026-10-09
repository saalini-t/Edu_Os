"""H2 step 1: upload admission. Size is enforced at the HTTP edge (declared AND actual bytes), pages / encryption /
validity by a sandboxed probe, all before anything is stored or queued."""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.knowledge import pdf as pdfmod
from app.knowledge.pdf import PdfError, probe_document, probe_pdf
from app.main import create_app
from app.upload_guard import FRAMING_ALLOWANCE, UploadSizeGuard
from tests.conftest import blank_pdf, login, make_pdf

LIMIT = 10_000


# ------------------------------------------------------------------ the ASGI guard, driven directly
def _drive(headers: list[tuple[bytes, bytes]], chunks: list[bytes], path="/v1/documents", method="POST"):
    """Run the guard around a recording app. Returns (status, body, app_called, bytes_the_app_read)."""
    state = {"called": False, "read": 0}

    async def app(scope, receive, send):
        state["called"] = True
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                return                                      # a real framework raises here; the guard answers instead
            state["read"] += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    feed = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent: list[dict] = []

    async def receive():
        return feed.pop(0) if feed else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    asyncio.run(UploadSizeGuard(app, LIMIT)(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, state["called"], state["read"]


def test_declared_content_length_over_limit_is_rejected_without_reading_the_body():
    big = LIMIT + FRAMING_ALLOWANCE + 1
    status, body, called, read = _drive([(b"content-length", str(big).encode())], [b"x" * 100])
    assert status == 413 and json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"
    assert not called and read == 0                                    # the application never saw the request


def test_missing_content_length_is_counted_while_streaming():
    chunk = b"x" * 4096
    status, body, called, read = _drive([], [chunk] * 100)             # chunked upload, 400 KB, no Content-Length
    assert status == 413 and json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"
    assert called and read <= LIMIT + FRAMING_ALLOWANCE                 # the app stopped reading at the limit


def test_understated_content_length_cannot_smuggle_a_larger_body():
    status, body, _, read = _drive([(b"content-length", b"10")], [b"x" * 4096] * 100)
    assert status == 413 and read <= LIMIT + FRAMING_ALLOWANCE


def test_malformed_content_length_falls_back_to_counting():
    status, *_ = _drive([(b"content-length", b"not-a-number")], [b"x" * 4096] * 100)
    assert status == 413


def test_body_within_the_limit_passes_through_untouched():
    status, _, called, read = _drive([(b"content-length", b"3000")], [b"x" * 1000] * 3)
    assert status == 201 and called and read == 3000


@pytest.mark.parametrize("path,method", [("/v1/auth/login", "POST"), ("/v1/documents", "GET"), ("/v1/doubts", "POST")])
def test_only_the_upload_routes_are_guarded(path, method):
    status, _, called, _ = _drive([(b"content-length", str(LIMIT * 100).encode())], [b"x"], path=path, method=method)
    assert called and status == 201


def test_replace_route_is_guarded_too():
    big = str(LIMIT + FRAMING_ALLOWANCE + 1).encode()
    assert _drive([(b"content-length", big)], [b"x"], path="/v1/documents/abc/file", method="PUT")[0] == 413


# ------------------------------------------------------------------ the full application
@pytest.fixture()
def small(settings):
    def build(**over):
        return TestClient(create_app(settings.model_copy(update=over)))
    return build


def _post(c, headers, course, data, name="d.pdf"):
    return c.post("/v1/documents", headers=headers, data={"course_id": course, "title": "h2 admission"},
                  files={"file": (name, io.BytesIO(data), "application/pdf")})


def test_size_boundary_exact_limit_accepted_one_byte_more_rejected(small, cn_course_id):
    data = make_pdf(["Boundary test paragraph about token windows and sliding frames."])
    ok = small(max_upload_bytes=len(data))
    assert _post(ok, login(ok, "student1@demo.local"), cn_course_id, data).status_code == 201
    tight = small(max_upload_bytes=len(data) - 1)
    r = _post(tight, login(tight, "student2@demo.local"), cn_course_id, data)
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_edge_rejection_carries_the_error_envelope_and_stores_nothing(small, cn_course_id, db):
    c = small(max_upload_bytes=1000)
    h = login(c, "student1@demo.local")
    before = db.execute(text("select count(*) from know.documents")).scalar()
    r = _post(c, h, cn_course_id, b"%PDF-1.4\n" + b"0" * (1000 + FRAMING_ALLOWANCE + 10))
    assert r.status_code == 413
    err = r.json()["error"]
    assert err["code"] == "FILE_TOO_LARGE" and err["details"]["max_bytes"] == 1000
    assert db.execute(text("select count(*) from know.documents")).scalar() == before


def test_page_budget_boundary(small, cn_course_id):
    c = small(max_pdf_pages=3)
    h = login(c, "student1@demo.local")
    assert _post(c, h, cn_course_id, make_pdf(["three page budget ok"] * 1)).status_code == 201
    over = blank_pdf(4)                                                  # 4 pages > 3: refused BEFORE parsing
    r = _post(c, h, cn_course_id, over)
    assert r.status_code == 422 and r.json()["error"]["code"] == "TOO_MANY_PAGES"
    assert r.json()["error"]["details"]["max_pages"] == 3
    exact = _post(c, h, cn_course_id, blank_pdf(3))          # exactly 3 pages passes admission (sync mode then fails: no text)
    assert exact.status_code == 422 and exact.json()["error"]["details"]["error_code"] == "NO_EXTRACTABLE_TEXT"


def test_encrypted_pdf_is_rejected_with_its_own_code(small, cn_course_id):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(make_pdf(["secret page text"]))).pages:
        w.add_page(p)
    w.encrypt("pw")
    buf = io.BytesIO()
    w.write(buf)
    c = small()
    r = _post(c, login(c, "student1@demo.local"), cn_course_id, buf.getvalue())
    assert r.status_code == 422 and r.json()["error"]["code"] == "ENCRYPTED_PDF"


def test_not_a_pdf_and_malformed_pdf_have_distinct_codes(small, cn_course_id):
    c = small()
    h = login(c, "student1@demo.local")
    assert _post(c, h, cn_course_id, b"PK\x03\x04 zip bytes").json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    r = _post(c, h, cn_course_id, b"%PDF-1.4\ngarbage that is not a document")
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNREADABLE_PDF"


def test_replace_applies_the_same_admission_rules(small, cn_course_id):
    c = small(max_pdf_pages=2)
    h = login(c, "student1@demo.local")
    doc = _post(c, h, cn_course_id, make_pdf(["replace admission original content"])).json()["document_id"]
    r = c.put(f"/v1/documents/{doc}/file", headers=h, files={"file": ("n.pdf", io.BytesIO(blank_pdf(3)), "application/pdf")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "TOO_MANY_PAGES"


# ------------------------------------------------------------------ the probe itself
def test_probe_document_codes():
    assert probe_document(blank_pdf(2), 5) == 2
    with pytest.raises(PdfError) as e:
        probe_document(blank_pdf(6), 5)
    assert e.value.code == "TOO_MANY_PAGES"
    with pytest.raises(PdfError) as e:
        probe_document(b"%PDF-1.4\nnope", 5)
    assert e.value.code == "UNREADABLE_PDF"


def test_probe_runs_in_the_sandboxed_child_process(settings):
    s = settings.model_copy(update={"parser_isolation": "subprocess", "max_pdf_pages": 5})
    assert probe_pdf(blank_pdf(3), s) == 3
    with pytest.raises(PdfError) as e:
        probe_pdf(blank_pdf(6), s)
    assert e.value.code == "TOO_MANY_PAGES"


def test_parser_timeout_scales_with_the_admitted_page_count(settings, monkeypatch):
    seen = {}
    monkeypatch.setattr(pdfmod, "_run_child", lambda params, data, timeout: seen.setdefault("t", timeout) and
                        {"ok": True, "pages": [], "ocr_engine": None})
    s = settings.model_copy(update={"parser_isolation": "subprocess", "parser_timeout_s": 100.0,
                                    "parser_timeout_per_page_s": 0.5})
    pdfmod.parse_document(b"x", s, pages=400)
    assert seen["t"] == 300.0


# ------------------------------------------------------------------ over a REAL socket (uvicorn), with a naive client
@pytest.fixture()
def live(settings):
    """The real application behind a real uvicorn server on an ephemeral port (limit: 100 kB)."""
    import threading
    import time as _t
    import uvicorn
    app = create_app(settings.model_copy(update={"max_upload_bytes": 100_000}))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        _t.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port
    server.should_exit = True
    th.join(10)


def _naive_post(port, token, course, data: bytes, *, chunked: bool, claim: int | None = None) -> tuple[int | None, bytes]:
    """Like curl / a browser: send the WHOLE body first, only then read the answer."""
    import socket
    b = "XBOUNDARYX"
    head = (f'--{b}\r\nContent-Disposition: form-data; name="course_id"\r\n\r\n{course}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="title"\r\n\r\nlive\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()
    body = head + data + f"\r\n--{b}--\r\n".encode()
    hdr = (f"POST /v1/documents HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
           f"Content-Type: multipart/form-data; boundary={b}\r\nConnection: close\r\n")
    hdr += "Transfer-Encoding: chunked\r\n\r\n" if chunked else f"Content-Length: {claim or len(body)}\r\n\r\n"
    s = socket.create_connection(("127.0.0.1", port), timeout=20)
    s.sendall(hdr.encode())
    try:
        if chunked:
            for i in range(0, len(body), 16384):
                part = body[i:i + 16384]
                s.sendall(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            s.sendall(b"0\r\n\r\n")
        else:
            s.sendall(body)
    except OSError:
        pass                                                    # a reset here is exactly the bug being tested for
    resp = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            resp += d
    except OSError:
        pass
    s.close()
    return (int(resp.split(b" ", 2)[1]) if resp.startswith(b"HTTP/") else None), resp


def test_real_server_oversize_is_answered_with_413_even_for_a_client_that_keeps_sending(live, settings, cn_course_id):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=20)
    c.request("POST", "/v1/auth/login", json.dumps({"email": "student1@demo.local", "password": "eduos-demo-2026"}),
              {"Content-Type": "application/json"})
    token = json.loads(c.getresponse().read())["access_token"]
    junk = b"%PDF-1.4\n" + b"0" * 5_000_000                       # 5 MB against a 100 kB limit
    for label, kw in (("declared length", dict(chunked=False)), ("chunked, no length", dict(chunked=True))):
        status, resp = _naive_post(live, token, cn_course_id, junk, **kw)
        assert status == 413 and b"FILE_TOO_LARGE" in resp, (label, status, resp[:200])


def test_real_server_understated_content_length_cannot_smuggle_a_body(live, cn_course_id):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=20)
    c.request("POST", "/v1/auth/login", json.dumps({"email": "student1@demo.local", "password": "eduos-demo-2026"}),
              {"Content-Type": "application/json"})
    token = json.loads(c.getresponse().read())["access_token"]
    status, resp = _naive_post(live, token, cn_course_id, b"%PDF-1.4\n" + b"0" * 2_000_000, chunked=False, claim=500)
    assert status in (400, 413, 422) and b"HTTP/1.1 20" not in resp.split(b"\r\n", 1)[0]       # never accepted as an upload


def test_real_server_accepts_a_normal_upload(live, cn_course_id):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=60)
    c.request("POST", "/v1/auth/login", json.dumps({"email": "student1@demo.local", "password": "eduos-demo-2026"}),
              {"Content-Type": "application/json"})
    token = json.loads(c.getresponse().read())["access_token"]
    status, resp = _naive_post(live, token, cn_course_id, make_pdf(["A normal small upload over a real socket about window scaling."]), chunked=False)
    assert status == 201
