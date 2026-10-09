from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.models import Base

config = context.config
target_metadata = Base.metadata
SCHEMAS = ("core", "orch", "know")


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        # know.chunk_embeddings needs the optional pgvector type and is created with raw SQL (migration 0002 and
        # app.knowledge.vectors), so autogenerate must never try to manage or drop it.
        return obj.schema in SCHEMAS and name != "chunk_embeddings"
    return True


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        for schema in SCHEMAS:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.commit()
        context.configure(connection=conn, target_metadata=target_metadata, include_schemas=True,
                          include_object=_include_object, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
