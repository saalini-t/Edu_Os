from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_ph = PasswordHasher()
# Verified against when the email is unknown, so login timing does not reveal valid emails.
_DUMMY_HASH = _ph.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    try:
        verified = _ph.verify(hashed or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(verified) and hashed is not None


def create_token(secret: str, user_id: uuid.UUID, role: str, ttl_minutes: int) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": now + timedelta(minutes=ttl_minutes)}
    return jwt.encode(payload, secret, algorithm="HS256"), ttl_minutes * 60


def decode_token(secret: str, token: str) -> dict:
    # Algorithm pinned: rejects "none" and algorithm-confusion tokens.
    return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "role"]})
