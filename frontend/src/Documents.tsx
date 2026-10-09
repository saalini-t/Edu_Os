import { FormEvent, useEffect, useRef, useState } from "react";
import { api, DocInfo, DocPhase, errText, UploadConfig } from "./api";
import { Async, Badge, Btn, Empty, Field, fmt, Notice, Page, useAction, useLoad } from "./ui";

const mb = (n: number) => `${Math.round(n / 1024 / 1024)} MB`;

// What each stable backend error code means for the person who sees it, and what they can do about it.
const PROBLEM: Record<string, string> = {
  FILE_TOO_LARGE: "The file is larger than the upload limit. Split the PDF or export a smaller version.",
  TOO_MANY_PAGES: "The PDF has more pages than the limit. Split it into parts (for example by chapter) and upload each part.",
  ENCRYPTED_PDF: "The PDF is password-protected. Remove the protection and upload it again.",
  UNREADABLE_PDF: "The file could not be read as a PDF. It may be damaged; re-export it and try again.",
  UNSUPPORTED_MEDIA_TYPE: "Only PDF files can be uploaded.",
  NO_EXTRACTABLE_TEXT: "This PDF has no readable text (it looks scanned). Text recognition (OCR) is not available or found nothing; ask an administrator.",
  EMBEDDING_FAILED: "Building the search index failed. Your file is safe; you can try again.",
  WORKER_LOST: "Processing was interrupted repeatedly. Your file is safe; you can try again.",
  MAX_ATTEMPTS_EXCEEDED: "Processing kept failing. You can try again later.",
  PARSER_TIMEOUT: "Reading the PDF took too long. You can try again, or upload a smaller part.",
  PARSER_CRASHED: "Reading the PDF failed. You can try again, or upload a smaller part.",
  PARSER_RESOURCE_LIMIT: "The PDF is too complex to read within the memory limit. Upload a smaller part.",
  DUPLICATE_CONTENT: "The same content already exists in this course.",
  FILE_MISSING: "The stored file is missing. Upload the document again.",
};
const problem = (code: string | null | undefined) => (code ? PROBLEM[code] ?? "Processing failed." : "Processing failed.");

const STEPS: { phase: DocPhase; label: string }[] = [
  { phase: "queued", label: "Uploaded" }, { phase: "parsing", label: "Reading pages" }, { phase: "chunking", label: "Splitting text" },
  { phase: "embedding", label: "Building search index" }, { phase: "ready", label: "Ready to search" },
];

function Progress({ d }: { d: DocInfo }) {
  const p = d.progress;
  if (!p) return null;
  const at = STEPS.findIndex((s) => s.phase === p.phase);
  const failed = p.phase === "failed";
  return (
    <div>
      <ol className="steps" aria-label="Processing steps">
        {STEPS.map((s, i) => (
          <li key={s.phase} className={!failed && i < at ? "done" : !failed && i === at ? (s.phase === "ready" ? "done" : "now") : ""}
            aria-current={!failed && i === at ? "step" : undefined}>
            {s.label}{s.phase === "embedding" && p.phase === "embedding" && p.percent != null ? ` ${p.percent}%` : ""}
          </li>))}
      </ol>
      {p.phase === "embedding" && p.chunks_total != null && (
        <>
          <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={p.percent ?? 0} aria-label="Search index progress">
            <i style={{ width: `${p.percent ?? 0}%` }} />
          </div>
          <span className="muted small">{p.chunks_embedded} of {p.chunks_total} passages indexed. This document becomes searchable when all of them are done.</span>
        </>)}
    </div>
  );
}

function DocRow({ d, onChanged }: { d: DocInfo; onChanged: () => void }) {
  const act = useAction();
  const ex = d.extraction;
  const unread = ex?.ocr_budget_exceeded_pages.length ?? 0;
  const empty = ex?.empty_pages.length ?? 0;
  const share = useAction();
  const tone = d.searchable ? "ok" : d.status === "FAILED" ? "bad" : "info";
  const label = d.searchable ? "Ready to search" : d.status === "FAILED" ? "Failed" : "Processing";
  return (
    <div className="card">
      <div className="row between">
        <div><b>{d.title}</b> <span className="muted small">{d.filename}{d.version > 1 ? ` · version ${d.version}` : ""}</span></div>
        <Badge tone={tone}>{label}</Badge>
      </div>
      <div className="row small" style={{ marginTop: 6 }}>
        <Badge tone={d.visibility === "course" ? "accent" : ""}>{d.visibility === "course" ? "Shared with the course" : "Private to you"}</Badge>
        {d.mine && (
          <Btn small kind="ghost" disabled={share.busy}
            onClick={() => share.run(() => api.setVisibility(d.document_id, d.visibility === "course" ? "private" : "course")).then(onChanged)}>
            {d.visibility === "course" ? "Make private" : "Share with the course"}
          </Btn>)}
        {!d.mine && <span className="muted">Shared by a classmate or teacher</span>}
      </div>
      {share.error && <Notice kind="error">{share.error}</Notice>}
      {d.visibility === "private" && d.mine && d.searchable &&
        <p className="muted small">Only you can get answers from this document. Share it to let your classmates and teachers use it too.</p>}
      {d.status !== "FAILED" && <Progress d={d} />}
      {d.progress?.reprocessing && <Notice>Updating this document. The previous version stays searchable until the new one is complete.</Notice>}
      {d.status === "FAILED" && (
        <Notice kind="error">
          <b>{problem(d.error_code)}</b>
          <div className="row" style={{ marginTop: 8 }}>
            {d.retryable
              ? <Btn small disabled={act.busy} onClick={() => act.run(() => api.retryDocument(d.document_id)).then(onChanged)}>{act.busy ? "Retrying…" : "Try again"}</Btn>
              : <span className="muted small">Retrying will not help; upload a corrected file.</span>}
            <Btn small kind="ghost" disabled={act.busy} onClick={() => act.run(() => api.deleteDocument(d.document_id)).then(onChanged)}>Remove</Btn>
          </div>
          {act.error && <div role="alert">{act.error}</div>}
        </Notice>)}
      {d.searchable && (
        <p className="muted small">
          {d.page_count ?? "?"} pages · {d.chunk_count ?? "?"} passages · all indexed · updated {fmt(d.updated_at)}
        </p>)}
      {d.searchable && unread > 0 && (
        <Notice kind="warn">{unread} scanned page{unread > 1 ? "s were" : " was"} beyond the text-recognition limit and {unread > 1 ? "are" : "is"} <b>not searchable</b> (pages {ex!.ocr_budget_exceeded_pages.slice(0, 8).join(", ")}{unread > 8 ? "…" : ""}).</Notice>)}
      {d.searchable && empty > 0 && <Notice>{empty} page{empty > 1 ? "s have" : " has"} no readable text (for example pictures only).</Notice>}
    </div>
  );
}

function UploadForm({ cfg, onUploaded }: { cfg: UploadConfig; onUploaded: (note: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [course, setCourse] = useState(cfg.courses[0]?.course_id ?? "");
  const [pct, setPct] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const pick = (f: File | null) => {
    setFile(f); setError(null);
    if (!f) return;
    if (!title) setTitle(f.name.replace(/\.pdf$/i, "").slice(0, 200));
    if (f.size > cfg.max_upload_bytes) setError(`${PROBLEM.FILE_TOO_LARGE} (This file is ${mb(f.size)}; the limit is ${mb(cfg.max_upload_bytes)}.)`);
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file || !course || error) return;
    setPct(0); setError(null);
    try {
      const r = await api.uploadDocument(course, title.trim() || file.name, file, setPct);
      onUploaded(r.created ? "Uploaded. We are processing it now; you can leave this page." : "This exact file was already uploaded. Showing the existing document.");
      setFile(null); setTitle(""); if (input.current) input.current.value = "";
    } catch (err) {
      const code = (err as { code?: string }).code;
      setError(code && PROBLEM[code] ? `${PROBLEM[code]}${code === "TOO_MANY_PAGES" ? ` (Limit: ${cfg.max_pdf_pages} pages.)` : ""}` : errText(err));
    } finally { setPct(null); }
  };
  if (!cfg.can_upload) return <Notice>Your role cannot upload documents.</Notice>;
  if (cfg.courses.length === 0) return <Notice kind="warn">You are not enrolled in a course, so there is nowhere to upload to yet.</Notice>;
  return (
    <form className="card" onSubmit={submit}>
      <h3>Upload a PDF</h3>
      <p className="muted small">Up to {mb(cfg.max_upload_bytes)} and {cfg.max_pdf_pages} pages. Text PDFs are searchable once every passage is indexed; large books take a few minutes.</p>
      <Field label="PDF file"><input ref={input} type="file" accept="application/pdf,.pdf" onChange={(e) => pick(e.target.files?.[0] ?? null)} /></Field>
      <Field label="Title"><input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Computer Networks, chapters 1–4" /></Field>
      {cfg.courses.length > 1 && (
        <Field label="Course"><select value={course} onChange={(e) => setCourse(e.target.value)}>
          {cfg.courses.map((c) => <option key={c.course_id} value={c.course_id}>{c.name}</option>)}</select></Field>)}
      {error && <Notice kind="error">{error}</Notice>}
      {pct !== null && (
        <div><div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Upload progress"><i style={{ width: `${pct}%` }} /></div>
          <span className="muted small">{pct < 100 ? `Uploading… ${pct}%` : "Checking the file…"}</span></div>)}
      <Btn type="submit" disabled={!file || !title.trim() || pct !== null || !!error}>{pct !== null ? "Uploading…" : "Upload"}</Btn>
    </form>
  );
}

export function Documents() {
  const cfg = useLoad(() => api.uploadConfig(), []);
  const [fast, setFast] = useState(false);
  const docs = useLoad(() => api.documents(), [], fast ? 2000 : 20000);   // poll quickly only while something is processing
  const [note, setNote] = useState<string | null>(null);
  const inflight = docs.data?.items.some((d) => d.status !== "READY" && d.status !== "FAILED" || d.progress?.reprocessing) ?? false;
  useEffect(() => { setFast(inflight); }, [inflight]);
  return (
    <Page title="Documents" sub="Upload course material. A document is searchable only after every passage is indexed." actions={<Btn kind="ghost" onClick={docs.reload}>Refresh</Btn>}>
      <Async res={cfg}>{(c) => <UploadForm cfg={c} onUploaded={(n) => { setNote(n); docs.reload(); }} />}</Async>
      {note && <Notice kind="success">{note}</Notice>}
      <Async res={docs} empty={(d) => (d.items.length ? null : <Empty>No documents yet. Upload a PDF to get started.</Empty>)}>
        {(d) => <div className="stack">{d.items.map((x) => <DocRow key={x.document_id} d={x} onChanged={docs.reload} />)}</div>}
      </Async>
    </Page>
  );
}
