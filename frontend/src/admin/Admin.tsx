import { useState } from "react";
import { api } from "../api";
import { Async, Badge, Btn, Empty, fmt, Notice, Page, Spinner, StatusBadge, Tabs, useAction, useLoad } from "../ui";

const Counts = ({ title, c }: { title: string; c: Record<string, number> }) => (
  <div className="card"><h4>{title}</h4>
    {Object.keys(c).length === 0 ? <span className="muted small">none</span> : <div className="row">{Object.entries(c).map(([k, v]) => <span key={k}><StatusBadge status={k} /> <b>{v}</b></span>)}</div>}</div>
);

export function Overview() {
  const res = useLoad(() => api.system(), [], 20000);
  const act = useAction();
  return (
    <Page title="System health" sub="What is actually running right now." actions={<Btn kind="ghost" onClick={res.reload}>Refresh</Btn>}>
      <Async res={res}>
        {(s) => (
          <>
            <div className="grid cols-2">
              <div className="card">
                <div className="row between"><h3>Language model</h3>{s.llm.reachable ? <Badge tone="ok">reachable</Badge> : <Badge tone="bad">not reachable</Badge>}</div>
                <p>Provider <b>{s.llm.provider}</b>{s.llm.model ? <> · model <b>{s.llm.model}</b></> : null}</p>
                {s.llm.provider === "fake"
                  ? <Notice kind="warn">Demo mode: explanations are extractive and nothing here says anything about real model quality.</Notice>
                  : !s.llm.reachable ? <Notice kind="error">The model cannot be reached. The system falls back to clarification or passages-only answers and records the fallback in each trace.</Notice> : null}
              </div>
              <div className="card">
                <div className="row between"><h3>Retrieval</h3>{s.retrieval.effective_mode === "hybrid" ? <Badge tone="ok">hybrid</Badge> : <Badge tone="warn">full-text only</Badge>}</div>
                <p>Configured <b>{s.retrieval.configured_mode}</b> · in effect <b>{s.retrieval.effective_mode}</b> · embeddings <b>{s.retrieval.embedding_provider}</b>{s.retrieval.embedding_model ? ` (${s.retrieval.embedding_model})` : ""}</p>
                {s.retrieval.fallback_reason && <p className="small muted">Why: {s.retrieval.fallback_reason}</p>}
                {s.retrieval.missing_embeddings ? <Notice kind="warn">{s.retrieval.missing_embeddings} chunk(s) have no embedding yet; they are found by full-text search only.</Notice> : null}
                <p className="small muted">Source verification: every explanation citation is checked against the retrieved chunk text before a student sees it (see traces).</p>
              </div>
            </div>
            <div className="grid cols-3">
              <Counts title="Documents" c={s.documents} /><Counts title="Ingestion jobs" c={s.ingestion_jobs} />
              <Counts title="Workflow runs" c={s.workflow_runs} /><Counts title="Escalations" c={s.escalations} />
            </div>
            <div className="card">
              <div className="row between"><div><h3>Pending workflow resumes: {s.pending_resumes}</h3><span className="muted small">Resolved cases whose workflow has not resumed yet. The worker retries these automatically.</span></div>
                <Btn disabled={act.busy} onClick={() => act.run(() => api.reconcile(), "Reconciled.").then(res.reload)}>Reconcile now</Btn></div>
              {act.error && <Notice kind="error">{act.error}</Notice>}{act.done && <Notice kind="success">{act.done}</Notice>}
            </div>
          </>
        )}
      </Async>
    </Page>
  );
}

export function Runs() {
  const runs = useLoad(() => api.runs(), []);
  const [rid, setRid] = useState<string | null>(null);
  return (
    <Page title="Workflow runs" sub="Every decision, with the rule that fired and the evidence it used." actions={<Btn kind="ghost" onClick={runs.reload}>Refresh</Btn>}>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card">
          <Async res={runs} empty={(d) => (d.items.length ? null : <Empty>No runs yet.</Empty>)}>
            {(d) => <table className="t"><thead><tr><th>Run</th><th>Status</th><th>When</th></tr></thead>
              <tbody>{d.items.map((r) => <tr key={r.run_id}><td><button className="linkish" onClick={() => setRid(r.run_id)}>{r.run_id.slice(0, 8)}</button> <span className="muted small">{r.student_ref}</span></td><td><StatusBadge status={r.status} /></td><td>{fmt(r.created_at)}</td></tr>)}</tbody></table>}
          </Async>
        </div>
        <div>{rid ? <TraceView rid={rid} /> : <Empty>Select a run to inspect its trace.</Empty>}</div>
      </div>
    </Page>
  );
}

function TraceView({ rid }: { rid: string }) {
  const t = useLoad(() => api.trace(rid), [rid]);
  const why = useLoad(async () => api.decisions((await api.trace(rid)).run.session_id), [rid]);
  return (
    <Async res={t}>
      {(tr) => (
        <div className="stack">
          <div className="card">
            <div className="row between"><h3>Run {tr.run.run_id.slice(0, 8)}</h3><StatusBadge status={tr.run.status} /></div>
            <p className="small">Rules fired: <b>{tr.summary.rules_fired.join(" → ") || "—"}</b><br />Providers: {tr.summary.providers_used.join(", ") || "none"} · outcome {tr.run.outcome ?? "—"}</p>
          </div>
          <div className="card"><h4>Decisions explained</h4>
            {why.loading ? <Spinner /> : why.error ? <Notice kind="error">{why.error}</Notice> : (
              <div className="stack">{why.data?.items.map((x) => (
                <div key={x.decision_id}>
                  <div className="row"><b>{x.title}</b><Badge tone={x.hard_rule ? "bad" : "accent"}>{x.rule_id}</Badge><Badge>{x.action}</Badge></div>
                  <ul className="small">{x.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
                  <p className="small muted">{x.precedence} Decision {x.decision_id.slice(0, 8)} · {x.provider ?? "no provider"}{x.model ? ` / ${x.model}` : ""}</p>
                  {x.evidence.length > 0 && <p className="small">Evidence: {x.evidence.map((e) => `${e.type} (${e.weight})`).join(", ")}</p>}
                  {x.fallbacks.length > 0 && <Notice kind="warn">Fallback in: {x.fallbacks.join(", ")}</Notice>}
                  {x.errors && x.errors.length > 0 && <Notice kind="error">{x.errors.map((e) => `${e.node}: ${e.error}`).join("; ")}</Notice>}
                </div>))}</div>)}
          </div>
          <div className="card"><h4>Steps</h4>
            <table className="t"><thead><tr><th>#</th><th>Node</th><th>Provider / model</th><th>ms</th></tr></thead>
              <tbody>{tr.steps.map((s) => <tr key={s.seq}><td>{s.seq}</td><td>{s.node}{s.error && <div className="small" style={{ color: "var(--bad)" }}>{s.error}</div>}</td><td>{s.provider ? `${s.provider} / ${s.model ?? ""} (${s.prompt_version ?? ""})` : "—"}</td><td>{s.latency_ms}</td></tr>)}</tbody></table></div>
          <div className="card"><h4>Retrieval</h4>
            {tr.retrieval.length === 0 ? <p className="muted small">No retrieval in this run.</p> : tr.retrieval.map((r, i) => (
              <div key={i}><p className="small">“{r.query}” · mode <b>{r.mode ?? "?"}</b></p>
                <ul className="small">{r.results.map((c) => <li key={c.chunk_id}>{c.chunk_id.slice(0, 8)} · page {c.page}</li>)}</ul></div>))}</div>
          <div className="card"><h4>Citation validation</h4>
            {tr.citation_validation.length === 0 ? <p className="muted small">No explanation in this run.</p> : tr.citation_validation.map((v, i) => (
              <div key={i}><p className="small"><b>{v.n_verified}</b> verified, <b>{v.n_stripped}</b> removed{v.fallback ? ` · fallback ${v.fallback}` : ""}</p>
                <ul className="small">{v.checks.map((c, j) => <li key={j}>{c.verified ? "✓" : "✗"} {c.chunk_id.slice(0, 8)} ({c.reason})</li>)}</ul></div>))}</div>
        </div>
      )}
    </Async>
  );
}

export function Ingestion() {
  const res = useLoad(() => api.ingestion(), [], 15000);
  return (
    <Page title="Ingestion jobs" sub="Document processing, retries and errors." actions={<Btn kind="ghost" onClick={res.reload}>Refresh</Btn>}>
      <div className="card">
        <Async res={res} empty={(d) => (d.items.length ? null : <Empty>No ingestion jobs yet.</Empty>)}>
          {(d) => <table className="t"><thead><tr><th>Document</th><th>Job</th><th>Status</th><th>Attempts</th><th>Error</th><th>Created</th></tr></thead>
            <tbody>{d.items.map((j) => (
              <tr key={j.job_id}><td>{j.title}<div className="small muted">{j.document_status}</div></td><td>{j.kind}{j.stage ? ` · ${j.stage}` : ""}</td><td><StatusBadge status={j.status} /></td>
                <td>{j.attempts}/{j.max_attempts}</td><td className="small">{j.error_code ? <><b>{j.error_code}</b> {j.last_error}</> : "—"}</td><td>{fmt(j.created_at)}</td></tr>))}</tbody></table>}
        </Async>
      </div>
    </Page>
  );
}

export function AdminEscalations() {
  const [tab, setTab] = useState<"ALL" | "OPEN" | "ACCEPTED">("ALL");
  const list = useLoad(() => api.adminEscalations(tab === "ALL" ? undefined : tab), [tab]);
  const [sel, setSel] = useState<string | null>(null);
  return (
    <Page title="Escalations" sub="Matching, assignment and the audit trail.">
      <Tabs value={tab} onChange={setTab} items={[{ id: "ALL", label: "All" }, { id: "OPEN", label: "Waiting" }, { id: "ACCEPTED", label: "Assigned" }]} />
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card">
          <Async res={list} empty={(d) => (d.items.length ? null : <Empty>No escalations in this view.</Empty>)}>
            {(d) => <table className="t"><thead><tr><th>Case</th><th>Status</th><th>Candidates</th></tr></thead>
              <tbody>{d.items.map((e) => <tr key={e.id}><td><button className="linkish" onClick={() => setSel(e.id)}>{e.topic ?? "no topic"}</button><div className="small muted">{e.doubt.slice(0, 60)}</div></td><td><StatusBadge status={e.status} /></td><td>{e.matched_candidates}</td></tr>)}</tbody></table>}
          </Async>
        </div>
        <div>{sel ? <EscDetail id={sel} onChange={list.reload} /> : <Empty>Select a case.</Empty>}</div>
      </div>
    </Page>
  );
}

function EscDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const res = useLoad(() => api.escalation(id), [id]);
  const teachers = useLoad(() => api.adminTeachers(), []);
  const [pick, setPick] = useState("");
  const act = useAction();
  return (
    <Async res={res}>
      {(e) => (
        <div className="stack">
          <div className="card">
            <div className="row between"><h3>{e.topic?.name ?? "Case"}</h3><StatusBadge status={e.status} /></div>
            <p className="small">Rule <b>{e.reason_rule_id}</b> · workflow resume <b>{e.resume?.status}</b>{e.resume?.outcome ? ` (${e.resume.outcome})` : ""}</p>
            <h4>Ranked candidates (deterministic)</h4>
            {(e.candidates?.length ?? 0) === 0 ? <Notice kind="warn">No matching teacher. The case stays open for every teacher of the course until it expires.</Notice> : (
              <table className="t"><thead><tr><th>#</th><th>Teacher</th><th>Score</th><th>topic · avail · lang · feedback · load</th></tr></thead>
                <tbody>{e.candidates!.map((c) => <tr key={c.teacher_id}><td>{c.rank}</td><td>{c.name}</td><td>{c.score}</td>
                  <td className="small">{["topic_fit", "availability", "language", "feedback", "load"].map((k) => c.components[k]?.toFixed(2)).join(" · ")}</td></tr>)}</tbody></table>)}
            {["OPEN", "ACCEPTED"].includes(e.status) && (
              <div className="row" style={{ marginTop: 10 }}>
                <select aria-label="Assign to teacher" value={pick} onChange={(x) => setPick(x.target.value)} style={{ width: "auto" }}>
                  <option value="">Assign to…</option>
                  {teachers.data?.items.filter((t) => t.active).map((t) => <option key={t.id} value={t.id}>{t.display_name} (load {t.open_load})</option>)}
                </select>
                <Btn small disabled={!pick || act.busy} onClick={() => act.run(() => api.assign(e.id, pick), "Assigned.").then((r) => { if (r) { res.reload(); onChange(); } })}>Assign / reassign</Btn>
              </div>)}
            {act.error && <Notice kind="error">{act.error}</Notice>}{act.done && <Notice kind="success">{act.done}</Notice>}
          </div>
          <div className="card"><h4>Audit trail</h4>
            <table className="t"><tbody>{e.events?.map((ev, i) => <tr key={i}><td>{fmt(ev.at)}</td><td><b>{ev.event}</b></td><td className="small mono">{JSON.stringify(ev.meta).slice(0, 120)}</td></tr>)}</tbody></table></div>
        </div>
      )}
    </Async>
  );
}
