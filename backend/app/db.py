from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool


def make_engine(url: str, *, pooled: bool = True) -> Engine:
    """`pooled=False` (NullPool) closes every connection on release; used for short-lived engines (tests build one app per
    test, and pooled engines would otherwise exhaust the database's connection limit)."""
    if not pooled:
        return create_engine(url, poolclass=NullPool, future=True)
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
