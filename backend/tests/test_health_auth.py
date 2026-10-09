import jwt
from sqlalchemy import text

from app.security import hash_password, verify_password
from tests.conftest import PASSWORD, ask, login, SLOW_START_Q


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_checks_real_dependencies(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"] == "ok" and body["checks"]["migrations"] == "ok"
    assert body["checks"]["storage"] == "ok"


def test_readyz_reports_unavailable_database(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    bad = settings.model_copy(update={"database_url": "postgresql+psycopg2://x:y@127.0.0.1:1/none"})
    r = TestClient(create_app(bad)).get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready" and r.json()["checks"]["database"].startswith("error")


def test_login_success_and_me(client):
    r = client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["user"]["role"] == "student"
    assert "password" not in r.text and "hash" not in r.text
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "student1@demo.local"
    assert "password_hash" not in me.json()


def test_login_rejects_bad_credentials(client):
    assert client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": "wrong"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "nobody@demo.local", "password": PASSWORD}).status_code == 401
    # no user enumeration: identical error body
    a = client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": "wrong"}).json()["error"]
    b = client.post("/v1/auth/login", json={"email": "nobody@demo.local", "password": "wrong"}).json()["error"]
    assert a["code"] == b["code"] and a["message"] == b["message"]


def test_passwords_are_hashed_with_argon2(db):
    rows = db.execute(text("select password_hash from core.users")).scalars().all()
    assert rows and all(h.startswith("$argon2") and PASSWORD not in h for h in rows)
    assert verify_password("pw", hash_password("pw")) and not verify_password("x", hash_password("pw"))


def test_protected_endpoints_require_authentication(client, cn_course_id):
    for method, path in [("get", "/v1/me"), ("get", "/v1/documents"), ("get", "/v1/doubts"),
                         ("get", "/v1/search?q=tcp"), ("get", "/v1/admin/runs"),
                         ("get", "/v1/admin/runs/00000000-0000-0000-0000-000000000000/trace")]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_invalid_expired_and_forged_tokens_rejected(client, settings):
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get("/v1/me", headers=bad).status_code == 401
    uid = client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": PASSWORD}).json()["user"]["id"]
    forged = jwt.encode({"sub": uid, "role": "admin", "exp": 4102444800}, "some-other-secret-0123456789012345", "HS256")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode({"sub": uid, "role": "student", "exp": 1}, settings.jwt_secret, "HS256")
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"
    none_alg = jwt.encode({"sub": uid, "role": "admin", "exp": 4102444800}, None, "none")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code == 401


def test_role_claim_in_token_does_not_grant_privileges(client, settings):
    """The role is read from the database, so a validly signed token with an inflated role claim gains nothing."""
    uid = client.post("/v1/auth/login", json={"email": "student1@demo.local", "password": PASSWORD}).json()["user"]["id"]
    tok = jwt.encode({"sub": uid, "role": "admin", "exp": 4102444800}, settings.jwt_secret, "HS256")
    assert client.get("/v1/admin/runs", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_role_based_access(client, student, teacher, admin, cn_course_id):
    assert client.get("/v1/admin/runs", headers=student).status_code == 403
    assert client.get("/v1/admin/runs", headers=teacher).status_code == 403
    assert client.get("/v1/admin/runs", headers=admin).status_code == 200
    assert ask(client, teacher, cn_course_id, SLOW_START_Q).status_code == 403   # doubts are student-only
    assert ask(client, admin, cn_course_id, SLOW_START_Q).status_code == 403
    assert client.get("/v1/doubts", headers=teacher).status_code == 403


def test_student_cannot_read_another_students_session(client, student, student2, admin, cn_course_id):
    sid = ask(client, student, cn_course_id, SLOW_START_Q).json()["session_id"]
    assert client.get(f"/v1/doubts/{sid}", headers=student).status_code == 200
    assert client.get(f"/v1/doubts/{sid}", headers=student2).status_code == 404   # no existence leak
    assert client.get(f"/v1/doubts/{sid}", headers=admin).status_code == 200
    assert client.get("/v1/doubts", headers=student2).json()["items"] == []
    assert client.post(f"/v1/doubts/{sid}/messages", headers=student2, json={"text": "hello there"}).status_code == 404
    assert client.post(f"/v1/doubts/{sid}/request-teacher", headers=student2).status_code == 404


def test_identity_is_never_taken_from_the_request_body(client, student, cn_course_id):
    uid = client.get("/v1/me", headers=login(client, "student2@demo.local")).json()["id"]
    r = client.post("/v1/doubts", headers={**student, "Idempotency-Key": "abcdefgh1"},
                    json={"course_id": cn_course_id, "text": SLOW_START_Q, "student_id": uid})
    assert r.status_code == 422   # unknown fields are rejected, so a spoofed student_id cannot be supplied


def test_error_envelope_has_trace_id(client):
    r = client.get("/v1/me", headers={"X-Trace-Id": "trace-abc"})
    assert r.json()["error"]["trace_id"] == "trace-abc" and r.headers["X-Trace-Id"] == "trace-abc"
