"""Controlled (re-)embedding. Idempotent and resumable.

    python -m app.knowledge.reindex                 # first run: register, embed all chunks, activate
    python -m app.knowledge.reindex --activate      # switch to a NEW configured model after fully embedding it
    python -m app.knowledge.reindex --purge-retired # delete vectors of retired models

Vectors from different models are never mixed: search only uses the single active model, and a configured model that
differs from the active one disables dense search (visibly) until this command is run with --activate."""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.knowledge import vectors
from app.knowledge.embeddings import EmbeddingUnavailable, get_embedding_provider
from app.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--activate", action="store_true", help="make the configured model active (required to switch models)")
    ap.add_argument("--purge-retired", action="store_true", help="delete embeddings of retired models")
    args = ap.parse_args(argv)
    setup_logging()
    s = get_settings()
    provider = get_embedding_provider(s)
    if provider is None:
        print("EMBEDDING_PROVIDER is 'none': nothing to index.", file=sys.stderr)
        return 2
    engine = make_engine(s.database_url)
    with make_session_factory(engine)() as db:
        if not vectors.ensure_vector_support(db):
            print("pgvector is not available on this PostgreSQL server.", file=sys.stderr)
            return 3
        try:
            dims = provider.dims
        except EmbeddingUnavailable as e:
            print(f"Embedding model unavailable: {e}", file=sys.stderr)
            return 4
        model = vectors.register_model(db, provider.model, dims)
        current = vectors.active_model(db)
        if current is not None and current.id != model.id and not args.activate:
            print(f"Active model is {current.name} ({current.dims} dims) but {model.name} is configured. "
                  "Re-run with --activate to embed and switch (search keeps using the active model until then).",
                  file=sys.stderr)
            return 5
        n = vectors.embed_missing(db, provider, model, batch_size=s.embedding_batch_size)
        missing = vectors.missing_count(db, model.id)
        if missing:
            print(f"{missing} chunk(s) still lack embeddings; not activating.", file=sys.stderr)
            return 6
        if current is None or current.id != model.id:
            vectors.activate_model(db, model)
        if args.purge_retired:
            print("purged retired vectors:", vectors.purge_retired(db))
        print(f"model={model.name} dims={model.dims} embedded_now={n} active=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
