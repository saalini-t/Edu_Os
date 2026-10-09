import { useState } from "react";
import { api, GapNode } from "../api";
import { Async, Badge, Empty, fmt, Page, pct, StatusBadge, useLoad } from "../ui";

const LEGEND: [string, string][] = [
  ["not_assessed", "No graded work yet"], ["suspected_gap", "A possible gap to check (not a diagnosis)"], ["practising", "Some graded work"],
  ["improving", "Going the right way"], ["demonstrated", "Shown across several different questions"],
];

function EvidenceList({ node }: { node: GapNode }) {
  if (!node.evidence.length) return <p className="muted small">No evidence recorded for this topic yet.</p>;
  return (
    <table className="t">
      <thead><tr><th>When</th><th>Evidence</th><th>Weight</th></tr></thead>
      <tbody>{node.evidence.map((e) => (
        <tr key={e.id}><td>{fmt(e.at)}</td><td>{e.type.replace(/_/g, " ")}{e.type.startsWith("self_report") && <span className="muted"> (self-report)</span>}</td><td>{e.weight === 0 ? "0 — does not count" : e.weight}</td></tr>
      ))}</tbody>
    </table>
  );
}

export function NodeDetail({ node, byId }: { node: GapNode; byId: Record<string, GapNode> }) {
  return (
    <div className="card" aria-live="polite">
      <div className="row between"><h3>{node.name}</h3><StatusBadge status={node.status} /></div>
      {node.gap_kind && <p><Badge tone={node.gap_kind === "confirmed" ? "bad" : "warn"}>{node.gap_kind === "confirmed" ? "Confirmed gap" : "Suspected gap (unconfirmed)"}</Badge></p>}
      <p><b>Why this status:</b> {node.reason}</p>
      {node.mean != null && <p className="small">Current estimate <b>{node.mean.toFixed(2)}</b> from {node.evidence_count} piece(s) of evidence{node.last_evidence_at ? `, latest ${fmt(node.last_evidence_at)}` : ""}.</p>}
      {node.attempts && <p className="small muted">{node.attempts.correct} of {node.attempts.attempts} graded answers correct.</p>}
      {node.hypotheses.length > 0 && (
        <div><h4>Hypotheses</h4><ul className="small">{node.hypotheses.map((h) => <li key={h.id}>{h.description} <Badge>{h.status === "proposed" ? "suspected" : h.status}</Badge></li>)}</ul></div>
      )}
      {node.check_prerequisites.length > 0 && (
        <p className="small"><b>Worth checking first:</b> {node.check_prerequisites.map((p) => `${p.name} (${p.status.replace(/_/g, " ")})`).join(", ")}. This is a suggestion from the course outline, not a diagnosis.</p>
      )}
      {node.prerequisites.length > 0 && <p className="small muted">Builds on: {node.prerequisites.map((id) => byId[id]?.name).filter(Boolean).join(", ")}</p>}
      <h4>Evidence behind this</h4>
      <EvidenceList node={node} />
    </div>
  );
}

export function GapMapView() {
  const res = useLoad(() => api.gapMap(), []);
  const [sel, setSel] = useState<string | null>(null);
  return (
    <Page title="Learning Gap Map" sub="Where each topic stands, based on your graded work.">
      <Async res={res} empty={(d) => (d.topics.length ? null : <Empty>This course has no topics yet.</Empty>)}>
        {(g) => {
          const byId = Object.fromEntries(g.topics.map((t) => [t.topic_id, t]));
          const node = g.topics.find((t) => t.slug === sel) ?? null;
          return (
            <>
              <div className="row" aria-label="Legend" style={{ marginBottom: 12 }}>
                {LEGEND.map(([s, d]) => <span key={s} title={d}><StatusBadge status={s} /> {g.counts[s] ?? 0}</span>)}
              </div>
              <div className="map" role="list">
                {g.topics.map((t) => (
                  <button key={t.slug} role="listitem" className={`node ${t.status}`} aria-pressed={sel === t.slug} onClick={() => setSel(t.slug === sel ? null : t.slug)}>
                    <b>{t.name}</b>
                    <div className="row" style={{ marginTop: 6 }}><StatusBadge status={t.status} />{t.gap_kind && <Badge tone={t.gap_kind === "confirmed" ? "bad" : "warn"}>{t.gap_kind}</Badge>}</div>
                    <div className="bar" aria-hidden="true"><i style={{ width: pct(t.mean) === "—" ? "0%" : pct(t.mean) }} /></div>
                    <span className="small muted">{t.evidence_count ? `${t.evidence_count} evidence · ${fmt(t.last_evidence_at)}` : "No evidence yet"}</span>
                  </button>
                ))}
              </div>
              <div style={{ marginTop: 16 }}>
                {node ? <NodeDetail node={node} byId={byId} /> : <p className="muted">Select a topic to see why it has its status and the evidence behind it.</p>}
              </div>
              <p className="muted small">{g.note}</p>
            </>
          );
        }}
      </Async>
    </Page>
  );
}

export function PassportView() {
  const res = useLoad(() => api.passport(), []);
  const [sel, setSel] = useState<string | null>(null);
  return (
    <Page title="Learning Passport" sub="A reproducible record of your learning, built from your evidence ledger.">
      <Async res={res}>
        {(p) => {
          const byId = Object.fromEntries(p.topics.map((t) => [t.topic_id, t]));
          const node = p.topics.find((t) => t.slug === sel) ?? null;
          return (
            <>
              <div className="grid cols-3">
                <div className="card"><div className="muted small">Doubts raised</div><h2>{p.totals.doubts}</h2></div>
                <div className="card"><div className="muted small">Graded answers</div><h2>{p.totals.graded_attempts}</h2></div>
                <div className="card"><div className="muted small">Still unresolved</div><h2>{p.totals.unresolved}</h2></div>
              </div>

              <div className="card">
                <h3>Topics</h3>
                <table className="t">
                  <thead><tr><th>Topic</th><th>Status</th><th>Answers</th><th>Evidence</th><th /></tr></thead>
                  <tbody>{p.topics.map((t) => (
                    <tr key={t.slug}><td>{t.name}</td><td><StatusBadge status={t.status} />{t.gap_kind && <> <Badge tone={t.gap_kind === "confirmed" ? "bad" : "warn"}>{t.gap_kind}</Badge></>}</td>
                      <td>{t.attempts ? `${t.attempts.correct}/${t.attempts.attempts}` : "0"}</td><td>{t.evidence_count}</td>
                      <td><button className="linkish" onClick={() => setSel(sel === t.slug ? null : t.slug)}>{sel === t.slug ? "Hide" : "Details"}</button></td></tr>
                  ))}</tbody>
                </table>
                {node && <div style={{ marginTop: 12 }}><NodeDetail node={node} byId={byId} /></div>}
              </div>

              <div className="card">
                <h3>Doubts and outcomes</h3>
                {p.doubts.length === 0 ? <Empty>No doubts yet.</Empty> : (
                  <table className="t"><thead><tr><th>When</th><th>Doubt</th><th>State</th><th /></tr></thead>
                    <tbody>{[...p.doubts].reverse().map((d) => (
                      <tr key={d.session_id}><td>{fmt(d.created_at)}</td><td>{d.text}</td>
                        <td>{d.unresolved ? <Badge tone="warn">Unresolved{d.outcome ? ` · ${d.outcome.toLowerCase()}` : ""}</Badge> : <Badge tone="ok">Resolved</Badge>}</td>
                        <td><a href={`#/doubt/${d.session_id}`}>Open</a></td></tr>
                    ))}</tbody></table>)}
              </div>

              {p.teacher_feedback.length > 0 && (
                <div className="card"><h3>Teacher feedback</h3>
                  {p.teacher_feedback.map((f) => <div key={f.escalation_id} className="card flat"><span className="muted small">{fmt(f.at)}</span><p>{f.notes}</p></div>)}</div>
              )}

              <div className="card">
                <h3>How your understanding changed</h3>
                {p.history.length === 0 ? <Empty>Changes appear here after graded practice or teacher feedback.</Empty> : (
                  <table className="t"><thead><tr><th>When</th><th>Topic</th><th>Status</th><th>Estimate</th><th>Caused by</th></tr></thead>
                    <tbody>{p.history.map((h, i) => (
                      <tr key={i}><td>{fmt(h.at)}</td><td>{h.topic}</td><td>{h.status}</td><td>{h.mean.toFixed(2)}</td><td>{h.evidence.type.replace(/_/g, " ")}</td></tr>
                    ))}</tbody></table>)}
              </div>
              <p className="muted small">{p.note} {p.retention}</p>
              <p className="muted small mono">Record digest {p.digest.slice(0, 16)}… (same evidence always gives the same digest)</p>
            </>
          );
        }}
      </Async>
    </Page>
  );
}
