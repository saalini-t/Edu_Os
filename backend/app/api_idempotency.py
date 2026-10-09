"""Shared idempotency helper for state-changing endpoints: same key + same body replays the stored response and applies
nothing; same key with a different body is rejected."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import IdempotencyKey


def run_idempotent(db: Session, *, scope: str, key: str, body: dict, fn: Callable[[], dict]) -> dict:
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    prior = db.get(IdempotencyKey, (scope, key))
    if prior is not None:
        if prior.request_hash != req_hash:
            raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was used with a different request")
        return prior.response
    response = jsonable_encoder(fn())
    db.add(IdempotencyKey(scope=scope, key=key, request_hash=req_hash, response=response))
    try:
        db.commit()
    except IntegrityError:               # a concurrent duplicate stored the key first: return its response
        db.rollback()
        prior = db.get(IdempotencyKey, (scope, key))
        if prior is None:
            raise
        return prior.response
    return response
