"""The 12-step acceptance scenario against the RUNNING Docker Compose stack, through the real HTTP API only
(plus one read-only `psql` lookup of answer keys so the script can deliberately fail questions).

    docker compose up -d --build
    python e2e/acceptance_scenario.py            # works with LLM_PROVIDER=fake (deterministic) or ollama

With the fake provider every assertion is exact. With a real model (ollama) the script asserts only the invariants that must
hold whatever the model writes (valid states, verified citations, keys hidden, evidence rules) and PRINTS what the model did;
it does not judge answer quality. Exits non-zero on the first failed check."""
import json
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8000"
PASSWORD = "eduos-demo-2026"
QUESTION = "Why does TCP slow start double the congestion window every RTT, but then stop doubling?"


def req(method, path, token=None, body=None, key=None, expect=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            status, out = resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        status, out = e.code, json.loads(e.read() or b"null")
    if expect is not None and status not in (expect if isinstance(expect, tuple) else (expect,)):
        fail(f"{method} {path} -> {status} {out}")
    return status, out


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        sys.exit(1)


def login(email):
    return req("POST", "/v1/auth/login", body={"email": email, "password": PASSWORD}, expect=200)[1]["access_token"]


def key_of(item_id):
    sql = f"select answer_key from core.practice_items where id = '{item_id}'"
    out = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "eduos", "-d", "eduos", "-At", "-c", sql],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    return out


def wrong_answer(item):
    if item["kind"] == "mcq":
        k = key_of(item["item_id"])
        return next(o for o in item["options"] if o != k)
    return "-1" if item["kind"] == "numeric" else "I do not know"


def main():
    stu, tea, adm = login("student1@demo.local"), login("teacher1@demo.local"), login("admin@demo.local")
    system = req("GET", "/v1/admin/system", adm, expect=200)[1]
    fake = system["llm"]["provider"] == "fake"
    print(f"provider={system['llm']['provider']} model={system['llm'].get('model')} retrieval={system['retrieval']['effective_mode']}")
    cid = req("GET", "/v1/documents", stu, expect=200)[1]["items"][0]["course_id"]

    print("1-3. doubt -> topic -> authorized retrieval -> validated, cited explanation")
    d = req("POST", "/v1/doubts", stu, {"course_id": cid, "text": QUESTION}, key=str(uuid.uuid4()), expect=202)[1]
    sid, rid = d["session_id"], d["run_id"]
    v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
    li = v["latest_intervention"]
    print(f"     status={v['status']} action={li['action']} provider={li.get('provider')} topic={(v['topic'] or {}).get('name')}")
    check(v["topic"] is not None, "topic identified")
    check(li["action"] == "GENERATE_EXPLANATION" and li["explanation"]["text"], "explanation produced")
    cites = li["explanation"]["citations"]
    check(all(c["verified"] for c in cites), f"every shown citation verified against retrieved chunks ({len(cites)} citation(s))")
    if fake:
        check(len(cites) >= 1, "fake provider cites at least one source")
    print("     explanation:", li["explanation"]["text"][:160].replace("\n", " "))

    print("4. targeted practice (answer keys hidden)")
    a = req("POST", f"/v1/doubts/{sid}/ack", stu, {"ack": "check_me"}, key=str(uuid.uuid4()), expect=202)[1]
    check(a["mastery_credit"] == 0.0, "acknowledgement carries zero mastery credit")
    v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
    check(v["status"] == "AWAITING_ANSWER" and v["practice"], f"practice generated ({sum(len(s['items']) for s in v['practice'])} items)")
    raw = json.dumps(v)
    check('"answer_key"' not in raw and '"rubric"' not in raw, "answer keys / rubrics are not in the student payload")

    print("5-7. answer evaluated exactly once; evidence and progress update; policy records why")
    items = v["practice"][-1]["items"]
    k = str(uuid.uuid4())
    first = req("POST", f"/v1/doubts/{sid}/answers", stu, {"item_id": items[0]["item_id"], "answer": wrong_answer(items[0]), "hints_used": 0}, key=k, expect=200)[1]
    again = req("POST", f"/v1/doubts/{sid}/answers", stu, {"item_id": items[0]["item_id"], "answer": wrong_answer(items[0]), "hints_used": 0}, key=k, expect=200)[1]
    check(first == again, "duplicate submission (same Idempotency-Key) replays the first result")
    st2, err = req("POST", f"/v1/doubts/{sid}/answers", stu, {"item_id": items[0]["item_id"], "answer": wrong_answer(items[0]), "hints_used": 0}, key=str(uuid.uuid4()))
    check(st2 == 409 and err["error"]["code"] == "ITEM_ALREADY_ANSWERED", "a second scored attempt on the same item is refused")
    check(first["scored"] and first["reveal"] is not None, "answer revealed only after submission")
    for it in items[1:]:
        req("POST", f"/v1/doubts/{sid}/answers", stu, {"item_id": it["item_id"], "answer": wrong_answer(it), "hints_used": 0}, key=str(uuid.uuid4()), expect=200)
    ev = req("GET", "/v1/learners/me/evidence", stu, expect=200)[1]["items"]
    graded = [e for e in ev if e["evidence_type"].startswith("attempt_")]
    v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
    counted = sum(1 for s in v["practice"] for i in s["items"] if (i["attempt"] or {}).get("counted_as_evidence"))
    check(len(graded) == counted and counted >= 1 and all(e["weight"] > 0 for e in graded),
          f"{len(graded)} graded evidence rows == {counted} attempts counted (uncertain model-graded answers are not counted)")
    check(all(e["weight"] == 0 for e in ev if e["evidence_type"].startswith("self_report")), "self-report rows have zero weight")

    print("8. repeated unsuccessful checks -> escalation rule")
    for _ in range(4):
        v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
        if v["status"] == "WAITING_HUMAN":
            break
        if v["status"] == "AWAITING_STUDENT":
            req("POST", f"/v1/doubts/{sid}/ack", stu, {"ack": "check_me"}, key=str(uuid.uuid4()), expect=202)
        elif v["status"] == "AWAITING_ANSWER":
            for it in v["practice"][-1]["items"]:
                if not (it["attempt"] or {}).get("scored"):
                    req("POST", f"/v1/doubts/{sid}/answers", stu, {"item_id": it["item_id"], "answer": wrong_answer(it), "hints_used": 0}, key=str(uuid.uuid4()), expect=200)
    v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
    check(v["status"] == "WAITING_HUMAN" and v["escalation"], f"workflow paused for a teacher (status {v['status']})")
    dec = req("GET", f"/v1/doubts/{sid}/decisions", stu, expect=200)[1]["items"]
    print("     rules:", " -> ".join(x["rule_id"] for x in dec))
    check(dec[-1]["rule_id"] in ("R5_repeated_failure", "R2_budget_exhausted"), f"escalation rule recorded: {dec[-1]['rule_id']}")
    check(any(x["evidence"] for x in dec), "decisions list the evidence they used")
    gm = req("GET", "/v1/learners/me/gap-map", stu, expect=200)[1]
    node = next(t for t in gm["topics"] if t["topic_id"] == v["topic"]["id"])
    check(node["status"] != "demonstrated" and node["evidence_count"] >= 1, f"gap map: {node['label']} ({node['evidence_count']} evidence)")

    print("9-10. suitable teacher receives the case; reply; resolution")
    eid = v["escalation"]["id"]
    inbox = req("GET", "/v1/teacher/escalations?status=OPEN", tea, expect=200)[1]["items"]
    check(eid in [e["id"] for e in inbox], "matched teacher sees the case in the inbox")
    t3 = login("teacher3@demo.local")
    check(req("GET", f"/v1/escalations/{eid}", t3)[0] == 404, "a teacher of another course gets 404")
    case = req("GET", f"/v1/escalations/{eid}", tea, expect=200)[1]
    check(len(case["brief"]["attempts"]) >= 2 and case["sources"], "case brief carries distinct attempts and course sources")
    req("POST", f"/v1/teacher/escalations/{eid}/accept", tea, expect=200)
    req("POST", f"/v1/escalations/{eid}/messages", tea, {"content": "Let's look at when slow start hands over to congestion avoidance."}, key=str(uuid.uuid4()), expect=201)
    req("POST", f"/v1/escalations/{eid}/messages", stu, {"content": "I mixed up cwnd and ssthresh."}, key=str(uuid.uuid4()), expect=201)
    tid = v["topic"]["id"]
    rk = str(uuid.uuid4())
    body = {"notes": "Walked through slow start vs congestion avoidance with a worked example.", "topic_assessments": [{"topic_id": tid, "level": "solid"}], "hypothesis_decisions": []}
    r1 = req("POST", f"/v1/teacher/escalations/{eid}/resolve", tea, body, key=rk, expect=200)[1]
    r2 = req("POST", f"/v1/teacher/escalations/{eid}/resolve", tea, body, key=rk, expect=200)[1]
    check(r1 == r2 and r1["workflow_resumed"], "resolution is idempotent and resumed the workflow")

    print("11-12. workflow resumed; student sees updated map and passport with reasons and evidence")
    v = req("GET", f"/v1/doubts/{sid}", stu, expect=200)[1]
    check(v["status"] == "COMPLETED" and v["latest_intervention"]["outcome"] == "RESOLVED", "run completed after the teacher's intervention")
    trace = req("GET", f"/v1/admin/runs/{rid}/trace", adm, expect=200)[1]
    check(trace["summary"]["rules_fired"][-1] == "RESUME_TEACHER_RESOLVED", "trace records the teacher's resolution")
    gm = req("GET", "/v1/learners/me/gap-map", stu, expect=200)[1]
    node = next(t for t in gm["topics"] if t["topic_id"] == tid)
    check(any(e["type"] == "teacher_assessment_solid" for e in node["evidence"]) and node["status"] != "demonstrated",
          f"teacher evidence linked, one assessment is not mastery: {node['label']}")
    pp = req("GET", "/v1/learners/me/passport", stu, expect=200)[1]
    pp2 = req("GET", "/v1/learners/me/passport", stu, expect=200)[1]
    check(pp["digest"] == pp2["digest"] and pp["teacher_feedback"] and pp["totals"]["doubts"] >= 1, "passport reproducible and includes teacher feedback")
    print("\nACCEPTANCE SCENARIO PASSED")


if __name__ == "__main__":
    main()
