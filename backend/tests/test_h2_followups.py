"""H2 follow-ups found by reviewing a real run: (1) clarification replies must never make the support gate stricter,
(2) a deterministic topic fallback when the model names none, (3) owners can share an upload with the course,
(4) refused uploads are recorded."""
from __future__ import annotations

import io

import pytest
from sqlalchemy import select, text

from app.agents.understanding import understand
from app.auth.principal import principal_for
from app.knowledge.service import PostgresFtsRetriever
from app.llm.schemas import DoubtAnalysis, TopicRef
from app.models import User
from tests.conftest import blank_pdf, login, make_pdf
from tests.test_agents import TOPICS, Scripted


# ------------------------------------------------------------------ 1. the support gate
def test_clarification_filler_does_not_raise_the_bar_for_a_good_source(db):
    p = principal_for(db, db.scalar(select(User).where(User.email == "student1@demo.local")))
    r = PostgresFtsRetriever(db, min_terms=4)
    original = "slow start cwnd"
    widened = original + " zzfillera zzfillerb zzfillerc zzfillerd"          # what the clarification rounds append
    strict = r.search(widened, p, top_k=6)                                     # old behaviour: need = min(4, 7 terms) = 4
    stable = r.search(widened, p, top_k=6, need_from=original)                 # need follows the ORIGINAL question: 3
    assert max(h.matched_terms for h in strict.hits) == 3 and strict.n_above_threshold == 0       # the defect
    assert stable.n_above_threshold >= 1 and stable.min_terms == 3
    assert [h.chunk_id for h in stable.hits] == [h.chunk_id for h in strict.hits]                 # ranking is untouched


def test_the_bar_is_never_lowered_below_the_original_question(db):
    p = principal_for(db, db.scalar(select(User).where(User.email == "student1@demo.local")))
    r = PostgresFtsRetriever(db, min_terms=4)
    res = r.search("slow start cwnd congestion window growth", p, need_from="slow start cwnd congestion window")
    assert res.min_terms == 4                                                  # a long original question still needs 4


# ------------------------------------------------------------------ 2. deterministic topic fallback
def _no_topic(clarity="ambiguous"):
    return Scripted(understand=lambda req, n: DoubtAnalysis(topic_id=None, clarity=clarity, classification_confidence=0.6))


def test_topic_fallback_picks_the_one_clear_winner_and_clears_a_two_keyword_question(settings):
    a = understand(_no_topic(), settings, doubt_text="what is cwnd and ssthresh", history=[], topics=TOPICS)
    assert a.analysis.topic_id == "t-cong" and a.analysis.clarity == "clear"
    assert any("keyword match" in n for n in a.run.notes)


def test_topic_fallback_with_one_hit_maps_the_topic_but_still_asks(settings):
    a = understand(_no_topic(), settings, doubt_text="what is a subnet", history=[], topics=TOPICS).analysis
    assert a.topic_id == "t-ip" and a.clarity == "ambiguous"                  # one keyword: topic known, question still vague


def test_topic_fallback_never_guesses_between_tied_topics(settings):
    tcp = [TopicRef(id="a", slug="a", name="TCP flow control", keywords=["tcp"]),
           TopicRef(id="b", slug="b", name="TCP congestion control", keywords=["tcp"])]
    a = understand(_no_topic(), settings, doubt_text="what is tcp", history=[], topics=tcp).analysis
    assert a.topic_id is None and a.clarity == "ambiguous"                    # genuinely ambiguous: still clarifies


def test_topic_fallback_ignores_unrelated_questions_and_failed_models(settings):
    assert understand(_no_topic(), settings, doubt_text="what is the capital of france", history=[], topics=TOPICS).analysis.topic_id is None
    broken = Scripted(understand=lambda req, n: RuntimeError("down"))
    out = understand(broken, settings, doubt_text="explain cwnd and ssthresh", history=[], topics=TOPICS)
    assert out.run.fallback and out.analysis.topic_id is None                 # a failed model still degrades to a clarification


def test_topic_fallback_does_not_override_a_valid_model_topic(settings):
    p = Scripted(understand=lambda req, n: DoubtAnalysis(topic_id="t-ip", clarity="clear", classification_confidence=0.9))
    assert understand(p, settings, doubt_text="explain cwnd and ssthresh", history=[], topics=TOPICS).analysis.topic_id == "t-ip"


# ------------------------------------------------------------------ 3. sharing an upload with the course
def _upload(client, h, course, data, title="shared-test"):
    return client.post("/v1/documents", headers=h, data={"course_id": course, "title": title},
                       files={"file": ("n.pdf", io.BytesIO(data), "application/pdf")})


def _found(client, h, course, q, doc_id):
    return any(r["document_id"] == doc_id for r in client.get("/v1/search", headers=h, params={"q": q, "course_id": course}).json()["results"])


def test_owner_can_share_and_unshare_and_others_follow_it(client, student, student2, cn_course_id, db):
    d = _upload(client, student, cn_course_id, make_pdf(["The sharetestzork protocol frames unique sharetestquux tokens."])).json()
    did = d["document_id"]
    assert d["visibility"] == "private" and d["mine"] is True
    assert not _found(client, student2, cn_course_id, "sharetestzork", did)
    assert client.get(f"/v1/documents/{did}", headers=student2).status_code == 404
    r = client.patch(f"/v1/documents/{did}/visibility", headers=student, json={"visibility": "course"})
    assert r.status_code == 200 and r.json()["visibility"] == "course" and r.json()["mine"] is True
    assert db.execute(text("select count(*) from know.chunks where document_id = :d and visibility <> 'course'"), {"d": did}).scalar() == 0
    assert _found(client, student2, cn_course_id, "sharetestzork", did)
    seen = client.get(f"/v1/documents/{did}", headers=student2).json()
    assert seen["mine"] is False and seen["visibility"] == "course"
    assert client.patch(f"/v1/documents/{did}/visibility", headers=student2, json={"visibility": "private"}).status_code in (403, 404)
    assert client.patch(f"/v1/documents/{did}/visibility", headers=student, json={"visibility": "private"}).status_code == 200
    assert not _found(client, student2, cn_course_id, "sharetestzork", did)
    audits = db.execute(text("select meta->>'visibility' from core.audit_events where action = 'document_visibility' "
                             "and entity_id = :d order by at"), {"d": did}).scalars().all()
    assert audits == ["course", "private"]


def test_visibility_rejects_unknown_values_and_strangers(client, student, student2, cn_course_id):
    did = _upload(client, student, cn_course_id, make_pdf(["Stranger test document about acknowledgement strangerzork tokens."]), "stranger").json()["document_id"]
    assert client.patch(f"/v1/documents/{did}/visibility", headers=student, json={"visibility": "public"}).status_code == 422
    assert client.patch(f"/v1/documents/{did}/visibility", headers=student2, json={"visibility": "course"}).status_code in (403, 404)
    assert client.get(f"/v1/documents/{did}", headers=student).json()["visibility"] == "private"


def test_a_reindex_keeps_the_shared_visibility_on_the_new_chunks(client, student, cn_course_id, db):
    did = _upload(client, student, cn_course_id, make_pdf(["Reindex sharing test reindexsharezork content for chunk visibility."]), "reidx-share").json()["document_id"]
    client.patch(f"/v1/documents/{did}/visibility", headers=student, json={"visibility": "course"})
    assert client.post(f"/v1/documents/{did}/reindex", headers=student).status_code == 200
    rows = db.execute(text("select distinct visibility, ingestion_version from know.chunks where document_id = :d"), {"d": did}).all()
    assert [tuple(r) for r in rows] == [("course", 2)]


# ------------------------------------------------------------------ 4. refused uploads are recorded
def test_a_refused_upload_is_audited_without_document_content(client, student, cn_course_id, db):
    r = _upload(client, student, cn_course_id, b"%PDF-1.4\ngarbage that cannot be parsed", "bad")
    assert r.status_code == 422
    row = db.execute(text("select meta from core.audit_events where action = 'document_upload_rejected' order by at desc limit 1")).scalar()
    assert row["code"] == "UNREADABLE_PDF" and row["http"] == 422 and row["bytes"] > 0 and "garbage" not in str(row)


def test_every_admission_code_is_audited(client, student, cn_course_id, db):
    _upload(client, student, cn_course_id, b"not a pdf at all", "x")                       # 415
    _upload(client, student, cn_course_id, blank_pdf(250), "pages")                        # TOO_MANY_PAGES (limit 200)
    codes = db.execute(text("select meta->>'code' from core.audit_events where action = 'document_upload_rejected' order by at")).scalars().all()
    assert codes[-2:] == ["UNSUPPORTED_MEDIA_TYPE", "TOO_MANY_PAGES"]


# ------------------------------------------------------------------ 5. prompt delimiters echoed by small models
def test_echoed_prompt_delimiters_never_reach_the_student(settings):
    p = Scripted(understand=lambda req, n: DoubtAnalysis(
        topic_id=None, clarity="ambiguous", classification_confidence=0.6,
        clarification_question="<doubt>\nexplain encapsulation in the context of computer networks</doubt>"))
    q = understand(p, settings, doubt_text="hmm", history=[], topics=TOPICS).analysis.clarification_question
    assert q == "explain encapsulation in the context of computer networks" and "<" not in q


def test_strip_delimiters_is_recursive_and_leaves_normal_text_alone():
    from app.agents.common import strip_delimiters
    assert strip_delimiters({"k": 1}) == {"k": 1}
    assert strip_delimiters(["<passage id=\"1\">TCP uses a <b>window</b></passage>", "a < b and c > d"]) == ["TCP uses a <b>window</b>", "a < b and c > d"]
