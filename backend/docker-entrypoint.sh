#!/bin/sh
# Retry-safe startup: no dependency on container start order. Migrations are idempotent.
set -e
if [ "$1" = "worker" ]; then
  # the API container runs migrations; the worker only starts after it is healthy (compose depends_on)
  exec python -m app.worker
fi
n=0
until alembic upgrade head; do
  n=$((n + 1))
  if [ "$n" -ge 30 ]; then echo "database not reachable after $n attempts" >&2; exit 1; fi
  echo "waiting for database (attempt $n)"; sleep 2
done
if [ "${SEED_ON_START:-false}" = "true" ]; then python -m app.seed; fi
# Embed + activate the configured model. Non-fatal: on failure the API serves full-text retrieval and /readyz says so.
if [ "${EMBEDDING_PROVIDER:-none}" != "none" ] && [ "${REINDEX_ON_START:-true}" = "true" ]; then
  python -m app.knowledge.reindex || echo "WARNING: embedding reindex failed; serving full-text retrieval" >&2
fi
exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port 8000
