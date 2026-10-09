"""End-to-end check of asynchronous ingestion against the running Docker Compose stack.

    docker compose up -d --build
    python e2e/compose_async_check.py

Scenarios: (1) upload -> 202 QUEUED -> worker indexes -> searchable; (2) worker stopped: document stays QUEUED, then
the restarted worker finishes it; (3) Redis stopped: upload still accepted and picked up by database polling.
It stops/starts the `worker` and `redis` services, so run it only against a disposable local stack."""
import json
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8000"
PASSWORD = "eduos-demo-2026"


def req(method, path, token=None, body=None, upload=None, form=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if upload:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in form.items()]
        name, content = upload
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f'Content-Type: application/pdf\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def login(email):
    return req("POST", "/v1/auth/login", body={"email": email, "password": PASSWORD})[1]["access_token"]


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True, cwd=ROOT)


def unique(path):
    """The same bytes are de-duplicated per owner, so make every run's upload distinct (trailing PDF comment)."""
    return Path(path).read_bytes() + b"\n%" + uuid.uuid4().hex.encode() + b"\n"


def wait_done(doc_id, token, secs=60):
    end, st = time.time() + secs, None
    while time.time() < end:
        st = req("GET", f"/v1/documents/{doc_id}", token)[1]
        if st["status"] in ("READY", "FAILED"):
            return st
        time.sleep(1)
    return st


def main():
    admin, student = login("admin@demo.local"), login("student1@demo.local")
    course = req("GET", "/v1/documents", student)[1]["items"][0]["course_id"]

    def upload(token, name, path):
        return req("POST", "/v1/documents", token, upload=(name, unique(path)), form={"course_id": course, "title": name})

    supplement = ROOT / "eval/data/corpus/computer_networks_supplement.pdf"
    seed = ROOT / "backend/seed/computer_networks.pdf"

    print("1) normal asynchronous flow")
    status, d = upload(admin, "supplement.pdf", supplement)
    print("   upload:", status, d["status"], "indexed =", d["indexed"], "| queue:", d["job"]["queue"])
    assert status == 202 and d["status"] == "QUEUED" and d["indexed"] is False
    assert d["job"]["queue"] == "notified", "Redis wake-up should have succeeded"
    st = wait_done(d["document_id"], admin)
    print("   final:", st["status"], "chunks =", st["chunk_count"], "| job:", st["job"]["status"], "attempts =", st["job"]["attempts"])
    assert st["status"] == "READY"
    q = urllib.parse.quote("DHCP discover offer request acknowledge")
    hits = req("GET", f"/v1/search?q={q}&course_id={course}", student)[1]["results"]
    assert any("DHCP" in h["text"] for h in hits)

    print("2) worker stopped: stays QUEUED; restarted worker finishes it")
    compose("stop", "worker")
    status, d2 = upload(student, "mine.pdf", seed)
    time.sleep(4)
    st = req("GET", f"/v1/documents/{d2['document_id']}", student)[1]
    print("   upload:", status, "| after 4s with no worker:", st["status"], "indexed =", st["indexed"])
    assert status == 202 and st["status"] == "QUEUED"
    compose("start", "worker")
    st = wait_done(d2["document_id"], student)
    print("   after worker restart:", st["status"], "attempts =", st["job"]["attempts"])
    assert st["status"] == "READY"

    print("3) Redis stopped: upload accepted, processed through database polling")
    compose("stop", "redis")
    try:
        status, d3 = upload(student, "noredis.pdf", seed)
        print("   upload:", status, "| queue:", d3["job"]["queue"])
        assert status == 202 and d3["job"]["queue"] == "deferred"
        st = wait_done(d3["document_id"], student)
        print("   final:", st["status"])
        assert st["status"] == "READY"
    finally:
        compose("start", "redis")
    print("ALL COMPOSE ASYNC CHECKS PASSED")


if __name__ == "__main__":
    main()
