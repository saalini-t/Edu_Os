"""The authenticated principal comes ONLY from the verified token, never from request fields.
The user (and role) is re-read from the database so deactivation takes effect immediately."""
from __future__ import annotations

import uuid
from collections.abc import Callable

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import AppError, Forbidden
from app.models import User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    if creds is None:
        raise AppError(401, "UNAUTHENTICATED", "Missing bearer token")
    try:
        claims = decode_token(request.app.state.settings.jwt_secret, creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except jwt.ExpiredSignatureError:
        raise AppError(401, "TOKEN_EXPIRED", "Token expired")
    except (jwt.PyJWTError, ValueError):
        raise AppError(401, "UNAUTHENTICATED", "Invalid token")
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise AppError(401, "UNAUTHENTICATED", "Invalid token")
    request.state.user_id = str(user.id)
    return user


def require_roles(*roles: str) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise Forbidden("Role not permitted for this endpoint")
        return user
    return dep
