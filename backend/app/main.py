from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.errors import install_error_handlers
from app.knowledge.router import router as knowledge_router
from app.learner.router import router as learner_router
from app.teaching.router import router as teaching_router
from app.llm.factory import build_provider
from app.logging_setup import setup_logging
from app.queue import WakeupQueue
from app.workflow.router import router as doubts_router

log = logging.getLogger("eduos.http")
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _head_revision() -> str:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


from app.ops import retrieval_info as _retrieval_info  # noqa: E402


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging()
    app = FastAPI(title="EduOS", version="0.1.0-m1")
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url, pooled=settings.app_env != "test")
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.wakeup_queue = WakeupQueue(settings.redis_url)
    app.state.llm_provider = build_provider(settings)
    if hasattr(app.state.llm_provider, "warm") and settings.app_env != "test":
        threading.Thread(target=app.state.llm_provider.warm, daemon=True, name="llm-warmup").start()   # non-blocking
    log.info("startup", extra={"llm_provider": app.state.llm_provider.name,
                               "env": settings.app_env})

    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Trace-Id"])

    @app.middleware("http")
    async def trace(request: Request, call_next):
        request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        log.info("request", extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                                   "ms": int((time.perf_counter() - start) * 1000),
                                   "trace_id": request.state.trace_id,
                                   "user_id": getattr(request.state, "user_id", None)})
        return response

    install_error_handlers(app)

    @app.get("/healthz", tags=["health"])
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz():
        checks: dict[str, str] = {}
        try:
            with app.state.engine.connect() as conn:
                conn.execute(text("select 1"))
                checks["database"] = "ok"
                current = MigrationContext.configure(conn).get_current_revision()
                checks["migrations"] = "ok" if current == _head_revision() else f"behind (at {current})"
        except Exception as e:
            checks["database"] = f"error: {type(e).__name__}"
            checks.setdefault("migrations", "unknown")
        try:
            d = Path(settings.storage_dir)
            d.mkdir(parents=True, exist_ok=True)
            probe = d / f".probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"x")
            probe.unlink()
            checks["storage"] = "ok"
        except Exception as e:
            checks["storage"] = f"error: {type(e).__name__}"
        ready = all(v == "ok" for v in checks.values())  # required dependencies only
        retrieval = _retrieval_info(app)
        try:
            llm = app.state.llm_provider.status()      # informational: an unreachable model degrades answers, not readiness
        except Exception as e:
            llm = {"provider": app.state.llm_provider.name, "reachable": False, "error": type(e).__name__}
        return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks, "retrieval": retrieval,
                             "llm": llm},
                            status_code=200 if ready else 503)

    for r in (auth_router, knowledge_router, doubts_router, learner_router, teaching_router, admin_router):
        app.include_router(r)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
