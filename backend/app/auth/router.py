from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
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
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or user is None or not user.active:
        db.add(AuditEvent(actor_id=None, action="login_failed", entity="user", entity_id=None, meta={}))
        db.commit()
        raise AppError(401, "UNAUTHENTICATED", "Invalid credentials")
    token, expires_in = create_token(s.jwt_secret, user.id, user.role, s.jwt_ttl_minutes)
    db.add(AuditEvent(actor_id=user.id, action="login", entity="user", entity_id=str(user.id), meta={}))
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in, "user": user_out(user)}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user_out(user)
