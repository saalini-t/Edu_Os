from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth.deps import current_user, require_roles
from app.auth.principal import principal_for
from app.db import get_db
from app.errors import AppError, Forbidden, NotFound
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from app.knowledge.ingestion import Ingestion
from app.knowledge.service import KnowledgeService
from sqlalchemy import select

from app.models import AuditEvent, Document, DocumentEvent, IngestionJob, User

router = APIRouter(prefix="/v1", tags=["knowledge"])


def job_out(j: IngestionJob | None) -> dict | None:
    if j is None:
        return None
    return {"job_id": str(j.id), "status": j.status, "stage": j.stage, "attempts": j.attempts,
            "max_attempts": j.max_attempts, "error_code": j.error_code, "last_error": j.last_error,
            "queue": "deferred" if j.enqueue_error else "notified", "created_at": j.created_at,
            "finished_at": j.finished_at}


def doc_out(d: Document, job: IngestionJob | None = None) -> dict:
    return {"document_id": str(d.id), "course_id": str(d.course_id), "title": d.title, "filename": d.filename,
            "status": d.status, "indexed": d.status == "READY", "visibility": d.visibility,
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
        doc = svc.ingest_pdf(owner_id=user.id, course_id=cid, visibility=visibility, title=title,
                             filename=file.filename or "upload.pdf", data=data)
        db.add(AuditEvent(actor_id=user.id, action="document_upload", entity="document", entity_id=str(doc.id),
                          meta={"status": doc.status, "error_code": doc.error_code, "mode": "sync"}))
        db.commit()
        if doc.status == "FAILED":
            raise AppError(422, "INGESTION_FAILED", "Document could not be ingested",
                           {"document_id": str(doc.id), "error_code": doc.error_code})
        return JSONResponse(jsonable_encoder(doc_out(doc, Ingestion(db, s).latest_job(doc.id))), status_code=201)
    ing = Ingestion(db, s)
    doc, job, created = ing.register_upload(owner_id=user.id, course_id=cid, visibility=visibility, title=title,
                                            filename=file.filename or "upload.pdf", data=data)
    if created and job is not None:
        err = request.app.state.wakeup_queue.notify(str(job.id))    # Redis down => job stays durable in PostgreSQL
        if err:
            job.enqueue_error = err
        db.add(AuditEvent(actor_id=user.id, action="document_upload", entity="document", entity_id=str(doc.id),
                          meta={"status": doc.status, "mode": "async", "queue": err or "notified"}))
        db.commit()
    # 202 = accepted for indexing; "indexed" stays false until the worker finishes (poll GET /v1/documents/{id})
    return JSONResponse(jsonable_encoder(doc_out(doc, job)), status_code=202 if created else 200)


@router.get("/documents")
def list_documents(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    docs = KnowledgeService(db, request.app.state.settings).list_documents(principal_for(db, user))
    return {"items": [doc_out(d) for d in docs]}


@router.get("/documents/{document_id}")
def get_document(document_id: str, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    svc = KnowledgeService(db, request.app.state.settings)
    doc = svc.get_document(_uuid(document_id, "document_id"))
    if doc is None or not svc.visible_to(doc, principal_for(db, user)):
        raise NotFound("Document")
    return doc_out(doc, Ingestion(db, request.app.state.settings).latest_job(doc.id))


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
    job = ing.register_replacement(doc, actor_id=user.id, filename=file.filename or "upload.pdf", data=data)
    if s.ingestion_mode == "sync" and ing.claim_job(job.id, "inline"):
        ing.process_job(job.id)
        db.refresh(doc)
        db.refresh(job)
    else:
        _queue_wakeup(request, db, job)
    return JSONResponse(jsonable_encoder(doc_out(doc, job)), status_code=200 if s.ingestion_mode == "sync" else 202)


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
    return JSONResponse(jsonable_encoder(doc_out(doc, job)), status_code=200 if s.ingestion_mode == "sync" else 202)


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
