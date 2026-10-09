from __future__ import annotations

import uuid

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_user, require_roles
from app.auth.principal import principal_for
from app.db import get_db
from app.errors import AppError, Forbidden, NotFound
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from app.knowledge.ingestion import RETRYABLE, Ingestion
from app.knowledge.service import KnowledgeService
from sqlalchemy import select

from app.models import AuditEvent, Course, Document, DocumentEvent, IngestionJob, User

router = APIRouter(prefix="/v1", tags=["knowledge"])


def job_out(j: IngestionJob | None) -> dict | None:
    if j is None:
        return None
    return {"job_id": str(j.id), "status": j.status, "stage": j.stage, "attempts": j.attempts,
            "max_attempts": j.max_attempts, "error_code": j.error_code, "last_error": j.last_error,
            "queue": "deferred" if j.enqueue_error else "notified", "created_at": j.created_at,
            "finished_at": j.finished_at}


class VisibilityIn(BaseModel):
    visibility: Literal["private", "course"]


def _rejected(db: Session, user: User, e: AppError, filename: str | None, nbytes: int, document_id: str = "") -> None:
    """A refused upload leaves no document, so record WHY (metadata only) where an admin can find it."""
    db.rollback()
    db.add(AuditEvent(actor_id=user.id, action="document_upload_rejected", entity="document", entity_id=document_id,
                      meta={"code": e.code, "http": e.status, "bytes": nbytes, "filename": (filename or "")[:80],
                            **{k: v for k, v in (e.details or {}).items() if isinstance(v, (int, str))}}))
    db.commit()


def doc_out(d: Document, job: IngestionJob | None = None, progress: dict | None = None, uid: uuid.UUID | None = None) -> dict:
    """`searchable` (alias `indexed`) is true only for READY: chunks AND, when embeddings are configured, every vector."""
    return {"document_id": str(d.id), "course_id": str(d.course_id), "title": d.title, "filename": d.filename,
            "status": d.status, "indexed": d.status == "READY", "searchable": d.status == "READY",
            "retryable": d.status == "FAILED" and d.error_code in RETRYABLE, "progress": progress,
            "visibility": d.visibility, "mine": uid is not None and d.owner_id == uid,
            "page_count": d.page_count, "chunk_count": d.chunk_count, "error_code": d.error_code,
            "version": d.ingestion_version, "extraction": _extraction_out(d.extraction_report),
            "created_at": d.created_at, "updated_at": d.updated_at, "job": job_out(job)}


def _extraction_out(rep: dict | None) -> dict | None:
    if not rep:
        return None
    return {"ocr_engine": rep.get("ocr_engine"), "ocr_pages": rep.get("ocr_pages", []),
            "empty_pages": rep.get("empty_pages", []), "failed_pages": rep.get("failed_pages", []),
            "ocr_budget_exceeded_pages": rep.get("ocr_budget_exceeded_pages", [])}


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AppError(422, "VALIDATION_ERROR", f"Invalid {what}")


@router.post("/documents")
def upload_document(request: Request, file: UploadFile = File(...), course_id: str = Form(...),
                    title: str = Form(..., min_length=1, max_length=200),
                    user: User = Depends(require_roles("student", "admin")), db: Session = Depends(get_db)):
    s = request.app.state.settings
    cid = _uuid(course_id, "course_id")
    principal = principal_for(db, user)
    if cid not in principal.allowed_course_ids:
        raise Forbidden("Not enrolled in this course")
    data = file.file.read(s.max_upload_bytes + 1)
    visibility = "course" if user.role == "admin" else "private"
    svc = KnowledgeService(db, s)
    if s.ingestion_mode == "sync":      # inline indexing (tests/dev): 201 only once the document is READY
        try:
            doc = svc.ingest_pdf(owner_id=user.id, course_id=cid, visibility=visibility, title=title,
                                 filename=file.filename or "upload.pdf", data=data)
        except AppError as e:
            _rejected(db, user, e, file.filename, len(data))
            raise
        db.add(AuditEvent(actor_id=user.id, action="document_upload", entity="document", entity_id=str(doc.id),
                          meta={"status": doc.status, "error_code": doc.error_code, "mode": "sync"}))
        db.commit()
        if doc.status == "FAILED":
            raise AppError(422, "INGESTION_FAILED", "Document could not be ingested",
                           {"document_id": str(doc.id), "error_code": doc.error_code})
        j = Ingestion(db, s).latest_job(doc.id)
        return JSONResponse(jsonable_encoder(doc_out(doc, j, Ingestion(db, s).progress(doc, j), user.id)), status_code=201)
    ing = Ingestion(db, s)
    try:
        doc, job, created = ing.register_upload(owner_id=user.id, course_id=cid, visibility=visibility, title=title,
                                                filename=file.filename or "upload.pdf", data=data)
    except AppError as e:
        _rejected(db, user, e, file.filename, len(data))
        raise
    if created and job is not None:
        err = request.app.state.wakeup_queue.notify(str(job.id))    # Redis down => job stays durable in PostgreSQL
        if err:
            job.enqueue_error = err
        db.add(AuditEvent(actor_id=user.id, action="document_upload", entity="document", entity_id=str(doc.id),
                          meta={"status": doc.status, "mode": "async", "queue": err or "notified"}))
        db.commit()
    # 202 = accepted for indexing; "indexed" stays false until the worker finishes (poll GET /v1/documents/{id})
    return JSONResponse(jsonable_encoder(doc_out(doc, job, ing.progress(doc, job), user.id)), status_code=202 if created else 200)


@router.get("/documents/upload-config")
def upload_config(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """What the upload form needs: the limits (advisory; the server enforces them) and ONLY the caller's own courses."""
    s = request.app.state.settings
    allowed = list(principal_for(db, user).allowed_course_ids)
    courses = db.scalars(select(Course).where(Course.id.in_(allowed)).order_by(Course.name)) if allowed else []
    return {"max_upload_bytes": s.max_upload_bytes, "max_pdf_pages": s.max_pdf_pages,
            "can_upload": user.role in ("student", "admin"),
            "courses": [{"course_id": str(c.id), "code": c.code, "name": c.name} for c in courses]}


@router.get("/documents")
def list_documents(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = request.app.state.settings
    ing = Ingestion(db, s)
    items = []
    for d in KnowledgeService(db, s).list_documents(principal_for(db, user)):
        inflight = d.status not in ("READY", "FAILED")
        j = ing.latest_job(d.id) if inflight else None
        items.append(doc_out(d, j, ing.progress(d, j) if inflight else None, user.id))     # progress only while in flight
    return {"items": items}


@router.get("/documents/{document_id}")
def get_document(document_id: str, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    svc = KnowledgeService(db, request.app.state.settings)
    doc = svc.get_document(_uuid(document_id, "document_id"))
    if doc is None or not svc.visible_to(doc, principal_for(db, user)):
        raise NotFound("Document")
    ing = Ingestion(db, request.app.state.settings)
    job = ing.latest_job(doc.id)
    return doc_out(doc, job, ing.progress(doc, job), user.id)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    svc = KnowledgeService(db, request.app.state.settings)
    doc = svc.get_document(_uuid(document_id, "document_id"))
    if doc is None or not svc.visible_to(doc, principal_for(db, user)):
        raise NotFound("Document")
    if doc.owner_id != user.id and user.role != "admin":
        raise Forbidden("Only the owner or an admin can delete a document")
    svc.delete_document(doc, actor_id=user.id)
    db.add(AuditEvent(actor_id=user.id, action="document_delete", entity="document", entity_id=document_id, meta={}))
    db.commit()


def _own_or_admin(db: Session, request: Request, user: User, document_id: str) -> Document:
    svc = KnowledgeService(db, request.app.state.settings)
    doc = svc.get_document(_uuid(document_id, "document_id"))
    if doc is None or not svc.visible_to(doc, principal_for(db, user)):
        raise NotFound("Document")
    if doc.owner_id != user.id and user.role != "admin":
        raise Forbidden("Only the owner or an admin can change a document")
    return doc


def _queue_wakeup(request: Request, db: Session, job: IngestionJob) -> None:
    err = request.app.state.wakeup_queue.notify(str(job.id))
    if err:
        job.enqueue_error = err
        db.commit()


@router.put("/documents/{document_id}/file")
def replace_document(document_id: str, request: Request, file: UploadFile = File(...),
                     user: User = Depends(require_roles("student", "admin")), db: Session = Depends(get_db)):
    """Replace the content of a document. The current version stays searchable until the new one is fully indexed;
    if indexing fails the current version is untouched."""
    s = request.app.state.settings
    doc = _own_or_admin(db, request, user, document_id)
    data = file.file.read(s.max_upload_bytes + 1)
    ing = Ingestion(db, s)
    try:
        job = ing.register_replacement(doc, actor_id=user.id, filename=file.filename or "upload.pdf", data=data)
    except AppError as e:
        _rejected(db, user, e, file.filename, len(data), str(doc.id))
        raise
    if s.ingestion_mode == "sync" and ing.claim_job(job.id, "inline"):
        ing.process_job(job.id)
        db.refresh(doc)
        db.refresh(job)
    else:
        _queue_wakeup(request, db, job)
    return JSONResponse(jsonable_encoder(doc_out(doc, job, ing.progress(doc, job), user.id)), status_code=200 if s.ingestion_mode == "sync" else 202)


@router.post("/documents/{document_id}/reindex")
def reindex_document(document_id: str, request: Request, user: User = Depends(require_roles("student", "admin")),
                     db: Session = Depends(get_db)):
    """Rebuild the chunks of the stored file (e.g. after OCR was enabled). Atomic swap; no duplicates."""
    s = request.app.state.settings
    doc = _own_or_admin(db, request, user, document_id)
    ing = Ingestion(db, s)
    job = ing.register_reindex(doc, actor_id=user.id)
    if s.ingestion_mode == "sync" and ing.claim_job(job.id, "inline"):
        ing.process_job(job.id)
        db.refresh(doc)
        db.refresh(job)
    else:
        _queue_wakeup(request, db, job)
    return JSONResponse(jsonable_encoder(doc_out(doc, job, ing.progress(doc, job), user.id)), status_code=200 if s.ingestion_mode == "sync" else 202)


@router.patch("/documents/{document_id}/visibility")
def set_document_visibility(document_id: str, body: VisibilityIn, request: Request,
                            user: User = Depends(require_roles("student", "admin")), db: Session = Depends(get_db)):
    """Owner (or admin) shares a document with the whole course (`course`) or takes it back (`private`). Audited."""
    doc = _own_or_admin(db, request, user, document_id)
    ing = Ingestion(db, request.app.state.settings)
    ing.set_visibility(doc, body.visibility, actor_id=user.id)
    db.add(AuditEvent(actor_id=user.id, action="document_visibility", entity="document", entity_id=document_id,
                      meta={"visibility": body.visibility}))
    db.commit()
    job = ing.latest_job(doc.id)
    return doc_out(doc, job, ing.progress(doc, job), user.id)


@router.post("/documents/{document_id}/retry")
def retry_document(document_id: str, request: Request, user: User = Depends(require_roles("student", "admin")),
                   db: Session = Depends(get_db)):
    """Re-run ingestion of a FAILED document whose failure was transient (embedding failed, worker lost, parser timeout...).
    Failures a retry cannot fix (encrypted, unreadable, no text) answer 409 NOT_RETRYABLE."""
    s = request.app.state.settings
    doc = _own_or_admin(db, request, user, document_id)
    ing = Ingestion(db, s)
    job = ing.register_retry(doc, actor_id=user.id)
    if s.ingestion_mode == "sync" and ing.claim_job(job.id, "inline"):
        ing.process_job(job.id)
        db.refresh(doc)
        db.refresh(job)
    else:
        _queue_wakeup(request, db, job)
    return JSONResponse(jsonable_encoder(doc_out(doc, job, ing.progress(doc, job), user.id)), status_code=200 if s.ingestion_mode == "sync" else 202)


@router.get("/admin/documents/{document_id}/events")
def document_events(document_id: str, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    """Lifecycle audit trail (works even after the document was deleted)."""
    did = _uuid(document_id, "document_id")
    rows = db.scalars(select(DocumentEvent).where(DocumentEvent.document_id == did).order_by(DocumentEvent.created_at))
    return {"items": [{"event": e.event, "actor_id": str(e.actor_id) if e.actor_id else None, "meta": e.meta,
                       "at": e.created_at} for e in rows]}


@router.get("/search")
def search(request: Request, q: str = Query(..., min_length=2, max_length=500),
           course_id: str | None = None, top_k: int = Query(6, ge=1, le=20),
           user: User = Depends(require_roles("student", "admin")), db: Session = Depends(get_db)):
    principal = principal_for(db, user)
    cid = _uuid(course_id, "course_id") if course_id else None
    if cid is not None and cid not in principal.allowed_course_ids:
        raise Forbidden("Not enrolled in this course")
    res = KnowledgeService(db, request.app.state.settings).retriever().search(q, principal, course_id=cid, top_k=top_k)
    return {"query": res.query, "method": res.method, "n_above_threshold": res.n_above_threshold,
            "min_terms": res.min_terms,
            "results": [{"chunk_id": h.chunk_id, "document_id": h.document_id, "document_title": h.document_title,
                         "course_id": h.course_id, "page": h.page, "text": h.text, "rank": round(h.rank, 4), "matched_terms": h.matched_terms}
                        for h in res.hits]}
