import io
import re
import uuid

import pytest
from sqlalchemy import text

from app.knowledge.chunker import chunk_pages, split_sentences
from tests.conftest import make_pdf

SEED_Q = "What is slow start and ssthresh in TCP congestion control?"


def upload(client, headers, course_id, data, name="notes.pdf", title="My notes", ctype="application/pdf"):
    return client.post("/v1/documents", headers=headers, data={"course_id": course_id, "title": title},
                       files={"file": (name, io.BytesIO(data), ctype)})


def test_seeded_document_is_ready(client, student):
    docs = client.get("/v1/documents", headers=student).json()["items"]
    assert len(docs) == 1
    d = docs[0]
    assert d["status"] == "READY" and d["page_count"] == 6 and d["chunk_count"] > 0 and d["error_code"] is None


def test_search_returns_passages_with_source_metadata(client, student, cn_course_id):
    r = client.get("/v1/search", headers=student, params={"q": SEED_Q, "course_id": cn_course_id})
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "postgres_fts" and body["results"]
    top = body["results"][0]
    assert {"chunk_id", "document_id", "document_title", "course_id", "page", "text", "rank"} <= set(top)
    assert "ssthresh" in " ".join(x["text"] for x in body["results"]).lower()
    assert all(x["course_id"] == cn_course_id for x in body["results"])
    pages = {x["page"] for x in body["results"]}
    assert 4 in pages          # the congestion control page of the seeded PDF


def test_search_validates_input(client, student):
    assert client.get("/v1/search", headers=student, params={"q": "x"}).status_code == 422
    assert client.get("/v1/search", headers=student, params={"q": "tcp", "course_id": "not-a-uuid"}).status_code == 422


def test_search_cannot_inject_tsquery_operators(client, student, cn_course_id):
    r = client.get("/v1/search", headers=student,
                   params={"q": "tcp & !( | ' ; drop table core.users; --", "course_id": cn_course_id})
    assert r.status_code == 200


def test_course_access_control_on_search(client, student3, cn_course_id):
    # student3 is enrolled only in the OS course
    assert client.get("/v1/search", headers=student3, params={"q": SEED_Q, "course_id": cn_course_id}).status_code == 403
    assert client.get("/v1/search", headers=student3, params={"q": SEED_Q}).json()["results"] == []
    assert client.get("/v1/documents", headers=student3).json()["items"] == []


def test_course_access_control_on_documents(client, student3, student, cn_course_id):
    doc_id = client.get("/v1/documents", headers=student).json()["items"][0]["document_id"]
    assert client.get(f"/v1/documents/{doc_id}", headers=student3).status_code == 404
    assert client.delete(f"/v1/documents/{doc_id}", headers=student3).status_code == 404
    assert upload(client, student3, cn_course_id, make_pdf(["Hello networks."])).status_code == 403


def test_private_upload_is_isolated_between_students(client, student, student2, cn_course_id):
    secret = "The zebracorn protocol uses quokka handshakes for frobnication."
    r = upload(client, student, cn_course_id, make_pdf([secret]), title="Private notes")
    assert r.status_code == 201 and r.json()["status"] == "READY" and r.json()["visibility"] == "private"
    q = "zebracorn quokka frobnication"
    own = client.get("/v1/search", headers=student, params={"q": q, "course_id": cn_course_id}).json()["results"]
    assert any("zebracorn" in x["text"] for x in own)
    other = client.get("/v1/search", headers=student2, params={"q": q, "course_id": cn_course_id}).json()["results"]
    assert not any("zebracorn" in x["text"] for x in other)
    doc_id = r.json()["document_id"]
    assert client.get(f"/v1/documents/{doc_id}", headers=student2).status_code == 404
    assert client.delete(f"/v1/documents/{doc_id}", headers=student2).status_code == 404


def test_upload_is_idempotent_by_content(client, student, cn_course_id):
    data = make_pdf(["Duplicate upload content about routers and switches."])
    a = upload(client, student, cn_course_id, data).json()
    b = upload(client, student, cn_course_id, data)
    assert b.status_code == 201 and b.json()["document_id"] == a["document_id"]


def test_delete_removes_document_and_chunks_from_search(client, student, cn_course_id, db):
    r = upload(client, student, cn_course_id, make_pdf(["Wibblewobble handshake frames are unique."]))
    doc_id = r.json()["document_id"]
    q = {"q": "wibblewobble", "course_id": cn_course_id}
    assert client.get("/v1/search", headers=student, params=q).json()["results"]
    assert client.delete(f"/v1/documents/{doc_id}", headers=student).status_code == 204
    assert client.get("/v1/search", headers=student, params=q).json()["results"] == []
    assert db.execute(text("select count(*) from know.chunks where document_id = :d"), {"d": doc_id}).scalar() == 0


def test_rejects_non_pdf_and_oversized_files(client, student, cn_course_id, settings):
    r = upload(client, student, cn_course_id, b"MZ\x90\x00 this is an exe", name="evil.pdf")
    assert r.status_code == 415 and r.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    r = upload(client, student, cn_course_id, b"just text", name="a.txt", ctype="text/plain")
    assert r.status_code == 415
    from fastapi.testclient import TestClient
    from app.main import create_app
    small = TestClient(create_app(settings.model_copy(update={"max_upload_bytes": 1000})))
    from tests.conftest import login
    h = login(small, "student1@demo.local")
    big = b"%PDF-1.4\n" + b"0" * 5000
    r = small.post("/v1/documents", headers=h, data={"course_id": cn_course_id, "title": "big"},
                   files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")})
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_unreadable_pdf_fails_clearly_and_is_recorded(client, student, cn_course_id):
    r = upload(client, student, cn_course_id, b"%PDF-1.4\nthis is not really a pdf at all")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INGESTION_FAILED" and err["details"]["error_code"] == "UNREADABLE_PDF"
    doc = client.get(f"/v1/documents/{err['details']['document_id']}", headers=student).json()
    assert doc["status"] == "FAILED" and doc["error_code"] == "UNREADABLE_PDF" and doc["chunk_count"] is None


def test_empty_pdf_fails_clearly(client, student, cn_course_id):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.showPage()
    c.save()   # one page, no text layer
    r = upload(client, student, cn_course_id, buf.getvalue())
    assert r.status_code == 422 and r.json()["error"]["details"]["error_code"] == "NO_EXTRACTABLE_TEXT"


def test_failed_documents_are_never_searchable(client, student, cn_course_id):
    upload(client, student, cn_course_id, b"%PDF-1.4\ngarbage with the word xylophonic congestion")
    r = client.get("/v1/search", headers=student, params={"q": "xylophonic", "course_id": cn_course_id})
    assert r.json()["results"] == []


def test_filename_cannot_escape_storage(client, student, cn_course_id, db, settings):
    r = upload(client, student, cn_course_id, make_pdf(["Path traversal attempt content for filenames."]),
               name="../../../../etc/evil.pdf")
    assert r.status_code == 201
    row = db.execute(text("select filename, storage_path from know.documents where id = :i"),
                     {"i": r.json()["document_id"]}).one()
    assert ".." not in row.filename and "/" not in row.filename
    assert re.fullmatch(r"[0-9a-f-]{36}\.pdf", row.storage_path)   # derived from the UUID only


# ---- chunker unit tests
def test_chunker_sentences_are_exact_substrings_and_pages_preserved():
    pages = ["First sentence here. Second sentence follows. " * 30, "Page two only has this sentence."]
    chunks = chunk_pages(pages, chunk_words=30, overlap_words=6)
    assert len(chunks) > 3
    assert {c.page for c in chunks} == {1, 2}
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert split_sentences(c.text)
        assert all(s in c.text for s in split_sentences(c.text))
        assert len(c.text.split()) <= 30 + 12   # a single over-long sentence may exceed the target slightly


def test_chunker_always_terminates_and_rejects_bad_overlap():
    assert chunk_pages(["One. Two. Three. Four. Five."], chunk_words=2, overlap_words=1)
    assert chunk_pages([""], 10, 2) == []
    with pytest.raises(ValueError):
        chunk_pages(["x"], 10, 10)


def test_equal_scores_are_ordered_by_stable_content_keys_not_random_ids(client, student, cn_course_id):
    """Two documents with identical text tie on every score. Ordering must follow (title, page, chunk), so evaluation
    runs are reproducible: random chunk UUIDs must never decide the order."""
    text_ = "Tiebreaker protocol zebrafish handshakes are described only in this paragraph about tiebreakerzork."
    for title in ("Tie B", "Tie A", "Tie C"):                      # uploaded in a different order than the title order
        assert upload(client, student, cn_course_id, make_pdf([text_]), title=title).status_code == 201
    for _ in range(3):
        r = client.get("/v1/search", headers=student, params={"q": "tiebreakerzork zebrafish", "course_id": cn_course_id}).json()["results"]
        assert [h["document_title"] for h in r if "tiebreakerzork" in h["text"]] == ["Tie A", "Tie B", "Tie C"]
