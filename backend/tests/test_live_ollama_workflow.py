"""Opt-in: the whole workflow against a REAL local model (OLLAMA_TEST_MODEL=qwen2.5:3b). It asserts that the system stays
safe and consistent whatever the model does (valid states, verified citations, answer keys hidden); it does NOT assert
answer quality, and it prints what the model actually did so a human can judge."""
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import SLOW_START_Q, _settings, ask, login
from tests.test_ack_evidence import ack
from tests.test_adaptive_flow import items, submit
from tests.test_workflow import trace

MODEL = os.environ.get("OLLAMA_TEST_MODEL")


def _up():
    try:
        return httpx.get("http://localhost:11434/api/tags", timeout=1.5).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not MODEL or not _up(), reason="set OLLAMA_TEST_MODEL with a local Ollama running")
def test_workflow_with_a_real_local_model(storage_dir, db, capsys):
    s = _settings(storage_dir, llm_provider="ollama", llm_model=MODEL, llm_base_url="http://localhost:11434", llm_timeout_s=240)
    client = TestClient(create_app(s), raise_server_exceptions=False)
    st, adm = login(client, "student1@demo.local"), login(client, "admin@demo.local")
    cid = client.get("/v1/documents", headers=st).json()["items"][0]["course_id"]
    r = ask(client, st, cid, SLOW_START_Q)
    assert r.status_code == 202, r.text
    sid, rid = r.json()["session_id"], r.json()["run_id"]
    view = client.get(f"/v1/doubts/{sid}", headers=st).json()
    li = view["latest_intervention"]
    t = trace(client, adm, rid)
    print("\n[ollama] status:", view["status"], "| action:", li["action"], "| provider:", li.get("provider"), "| fallback:", li.get("fallback"))
    print("[ollama] steps:", [(x["node"], x["provider"], x["error"]) for x in t["steps"]])
    print("[ollama] understand:", t["steps"][0]["output"], "| decisions:", t["decisions"])
    if li.get("explanation"):
        print("[ollama] explanation:", li["explanation"]["text"][:300])
        assert all(c["verified"] for c in li["explanation"]["citations"])        # unverified citations never reach the student
    assert view["status"] in ("AWAITING_STUDENT", "WAITING_HUMAN", "COMPLETED")
    if view["status"] == "AWAITING_STUDENT" and li.get("explanation"):
        a = ack(client, st, sid, "understood")
        print("[ollama] ack ->", a.status_code, a.json().get("status"))
        if a.json().get("status") == "AWAITING_ANSWER":
            its = items(db, sid)
            print("[ollama] practice:", [(i.kind, i.prompt[:90]) for i in its])
            shown = client.get(f"/v1/doubts/{sid}", headers=st).text
            assert "answer_key" not in shown and "rubric" not in shown      # keys stay server-side (their text may legitimately echo the explanation)
            res = submit(client, st, sid, its[0].id, its[0].answer_key)
            print("[ollama] submit key ->", res.status_code, res.json())
            assert res.status_code == 200
