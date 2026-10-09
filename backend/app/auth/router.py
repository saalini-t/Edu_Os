from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import current_user
from app.db import get_db
from app.errors import AppError
from app.models import AuditEvent, User
from app.security import create_token, verify_password

router = APIRouter(prefix="/v1", tags=["auth"])


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    role: str
    display_name: str
    email: str


def user_out(u: User) -> UserOut:
    return UserOut(id=str(u.id), role=u.role, display_name=u.display_name, email=u.email)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    s = request.app.state.settings
    email = body.email.strip().lower()
    email_sha = hashlib.sha256(email.encode()).hexdigest()[:16]          # pseudonymous key: the address itself is never stored
    if s.login_max_failures:
        since = datetime.now(timezone.utc) - timedelta(minutes=s.login_window_minutes)
        recent = db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "login_failed", AuditEvent.at >= since, AuditEvent.meta["email_sha"].astext == email_sha)) or 0
        if recent >= s.login_max_failures:                              # checked BEFORE the password: no guessing oracle
            wait = s.login_window_minutes * 60
            raise AppError(429, "RATE_LIMITED", "Too many failed sign-in attempts. Try again later.",
                           {"retry_after_s": wait}, headers={"Retry-After": str(wait)})
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or user is None or not user.active:
        db.add(AuditEvent(actor_id=None, action="login_failed", entity="user", entity_id=None, meta={"email_sha": email_sha}))
        db.commit()
        raise AppError(401, "UNAUTHENTICATED", "Invalid credentials")
    token, expires_in = create_token(s.jwt_secret, user.id, user.role, s.jwt_ttl_minutes)
    db.add(AuditEvent(actor_id=user.id, action="login", entity="user", entity_id=str(user.id), meta={}))
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in, "user": user_out(user)}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user_out(user)
