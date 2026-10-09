"""Consistent API error envelope (docs/API_CONTRACTS.md section 1.1)."""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("eduos.errors")


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def NotFound(what: str = "Resource") -> AppError:  # noqa: N802
    return AppError(404, "NOT_FOUND", f"{what} not found")


def Forbidden(message: str = "Not allowed") -> AppError:  # noqa: N802
    return AppError(403, "FORBIDDEN", message)


def _body(request: Request, code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {},
                      "trace_id": getattr(request.state, "trace_id", None)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return JSONResponse(_body(request, exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        code = {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                405: "METHOD_NOT_ALLOWED", 413: "FILE_TOO_LARGE", 415: "UNSUPPORTED_MEDIA_TYPE"}.get(
            exc.status_code, "ERROR")
        return JSONResponse(_body(request, code, str(exc.detail)), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errs = [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(_body(request, "VALIDATION_ERROR", "Request validation failed", {"errors": errs}),
                            status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"trace_id": getattr(request.state, "trace_id", None)})
        return JSONResponse(_body(request, "INTERNAL_ERROR", "Internal server error"), status_code=500)
