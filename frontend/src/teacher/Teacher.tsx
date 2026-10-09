import { FormEvent, useState } from "react";
import { api, EscalationView, idemKey } from "../api";
import { Thread } from "../Thread";
import { Async, Badge, Btn, Empty, fmt, Field, Notice, Page, pct, StatusBadge, Tabs, useAction, useLoad } from "../ui";

type Tab = "OPEN" | "ACCEPTED" | "DONE";

export function Inbox() {
  const [tab, setTab] = useState<Tab>("OPEN");
  const open = useLoad(() => api.inbox("OPEN"), [], 30000);
  const mine = useLoad(() => api.inbox("ACCEPTED"), [], 30000);
  const resolved = useLoad(() => api.inbox("RESOLVED"), []);
  const expired = useLoad(() => api.inbox("EXPIRED"), []);
  const cur = tab === "OPEN" ? open : tab === "ACCEPTED" ? mine : null;
  const doneRows = [...(resolved.data?.items ?? []), ...(expired.data?.items ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return (
    <Page title="Case inbox" sub="Doubts that need a person. You only see courses and cases you are authorised for."
      actions={<Btn kind="ghost" onClick={() => { open.reload(); mine.reload(); resolved.reload(); expired.reload(); }}>Refresh</Btn>}>
      <Tabs value={tab} onChange={setTab} items={[
        { id: "OPEN", label: `Waiting (${open.data?.items.length ?? "…"})` }, { id: "ACCEPTED", label: `Mine (${mine.data?.items.length ?? "…"})` }, { id: "DONE", label: "Done" }]} />
      {cur ? (
        <Async res={cur} empty={(d) => (d.items.length ? null : <Empty>{tab === "OPEN" ? "No cases are waiting. New ones appear here when a student needs a teacher." : "You have no active cases."}</Empty>)}>
          {(d) => <Rows rows={d.items} />}
        </Async>
      ) : resolved.loading || expired.loading ? <Async res={resolved}>{() => null}</Async>
        : doneRows.length === 0 ? <Empty>No finished cases yet.</Empty> : <Rows rows={doneRows} />}
    </Page>
  );
}

function Rows({ rows }: { rows: { id: string; status: string; topic: string | null; doubt: string; created_at: string; expires_at: string; reason_rule_id: string }[] }) {
  return (
    <div className="stack">{rows.map((r) => (
      <a key={r.id} href={`#/case/${r.id}`} className="card" style={{ display: "block", textDecoration: "none", color: "inherit" }}>
        <div className="row between"><b>{r.topic ?? "Topic not identified"}</b><StatusBadge status={r.status} /></div>
        <p style={{ margin: "6px 0" }}>{r.doubt}</p>
        <span className="muted small">Opened {fmt(r.created_at)} · {r.reason_rule_id.replace(/_/g, " ")}{r.status === "OPEN" || r.status === "ACCEPTED" ? ` · expires ${fmt(r.expires_at)}` : ""}</span>
      </a>))}</div>
  );
}

function ResolveForm({ e, onDone }: { e: EscalationView; onDone: () => void }) {
  const [notes, setNotes] = useState("");
  const [level, setLevel] = useState<"" | "struggling" | "emerging" | "solid">("");
  const [dec, setDec] = useState<Record<string, "" | "confirmed" | "refuted">>({});
  const [key] = useState(idemKey);                       // one key per form: a double-click cannot resolve twice
  const act = useAction();
  const topicId = e.topic?.id ?? e.brief?.topic?.id;
  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const r = await act.run(() => api.resolve(e.id, {
      notes: notes.trim(),
      topic_assessments: level && topicId ? [{ topic_id: topicId, level }] : [],
      hypothesis_decisions: Object.entries(dec).filter(([, d]) => d).map(([hypothesis_id, decision]) => ({ hypothesis_id, decision: decision as string })),
    }, key));
    if (r) onDone();
  };
  const hyps = e.live?.hypotheses.filter((h) => h.status === "proposed") ?? [];
  return (
    <form className="card" onSubmit={submit}>
      <h3>Resolve this case</h3>
      <p className="muted small">Your note is shown to the student. Your assessment becomes bounded evidence in their record; a single assessment never marks mastery.</p>
      <Field label="Resolution note for the student"><textarea value={notes} onChange={(x) => setNotes(x.target.value)} maxLength={4000} required /></Field>
      {topicId && (
        <Field label={`Your assessment of ${e.topic?.name ?? "the topic"} (optional)`} hint="“Emerging” is recorded for the history but carries no weight.">
          <select value={level} onChange={(x) => setLevel(x.target.value as typeof level)}>
            <option value="">No assessment</option><option value="struggling">Struggling</option><option value="emerging">Emerging</option><option value="solid">Solid</option>
          </select>
        </Field>)}
      {hyps.map((h) => (
        <Field key={h.id} label={`Suspected gap: “${h.description}”`}>
          <select value={dec[h.id] ?? ""} onChange={(x) => setDec({ ...dec, [h.id]: x.target.value as "" | "confirmed" | "refuted" })}>
            <option value="">Leave as suspected</option><option value="confirmed">Confirm this gap</option><option value="refuted">Refute it</option>
          </select>
        </Field>))}
      {act.error && <Notice kind="error">{act.error}</Notice>}
      <Btn type="submit" disabled={act.busy || !notes.trim()}>{act.busy ? "Saving…" : "Resolve and resume the student's workflow"}</Btn>
    </form>
  );
}

export function Case({ id, go }: { id: string; go: (to: string) => void }) {
  const res = useLoad(() => api.escalation(id), [id], 20000);
  const act = useAction();
  return (
    <Page title="Case" actions={<a className="btn ghost" href="#/">Back to inbox</a>}>
      <Async res={res}>
        {(e) => {
          const b = e.brief;
          const doIt = async (fn: () => Promise<unknown>, ok: string) => { const r = await act.run(fn, ok); if (r !== undefined) res.reload(); };
          return (
            <>
              <div className="row" style={{ marginBottom: 12 }}><StatusBadge status={e.status} />{e.topic && <Badge tone="accent">{e.topic.name}</Badge>}
                <span className="muted small">{e.student?.display_name} · opened {fmt(e.created_at)} · expires {fmt(e.expires_at)}</span></div>
              {act.error && <Notice kind="error">{act.error}</Notice>}
              {act.done && <Notice kind="success">{act.done}</Notice>}
              <div className="row" style={{ marginBottom: 12 }}>
                {e.can_accept && <Btn disabled={act.busy} onClick={() => doIt(() => api.accept(e.id), "You accepted this case.")}>Accept case</Btn>}
                {e.status === "ACCEPTED" && e.assigned_to_me && <Btn kind="ghost" disabled={act.busy} onClick={() => doIt(() => api.release(e.id), "Released back to the queue.")}>Release</Btn>}
              </div>
              {b && (
                <div className="grid cols-2">
                  <div className="card">
                    <h3>The student’s doubt</h3><p>{b.doubt}</p>
                    {b.follow_ups.length > 0 && <><h4>Follow-ups</h4><ul className="small">{b.follow_ups.map((f, i) => <li key={i}>{f}</li>)}</ul></>}
                    <p className="small muted"><b>Why it was escalated:</b> {b.reason.rule_id.replace(/_/g, " ")}. {b.reason.reasons.join(" ")}</p>
                  </div>
                  <div className="card">
                    <h3>Learner context</h3>
                    {e.live?.mastery ? <p>Status <StatusBadge status={e.live.mastery.status} /> · estimate {e.live.mastery.evidence_count ? pct(e.live.mastery.mean) : "—"} from {e.live.mastery.evidence_count} evidence item(s)</p> : <p className="muted">No topic evidence.</p>}
                    {(e.live?.hypotheses.length ?? 0) > 0 && <ul className="small">{e.live!.hypotheses.map((h) => <li key={h.id}>{h.description} <Badge>{h.status === "proposed" ? "suspected" : h.status}</Badge></li>)}</ul>}
                    <p className="small muted">Self-reported “I understood” clicks carry no weight and are not shown as progress.</p>
                  </div>
                </div>)}
              {b && (
                <div className="card">
                  <h3>What the system already tried</h3>
                  {b.explanation_given ? <><p className="small muted">Explanation given:</p><blockquote className="small">{b.last_explanation || "(not stored)"}</blockquote></> : <p className="muted">No explanation was given.</p>}
                  <h4>Practice attempts (distinct questions)</h4>
                  {b.attempts.length === 0 ? <p className="muted small">No practice attempts.</p> : (
                    <table className="t"><thead><tr><th>Question</th><th>Answer</th><th>Result</th></tr></thead>
                      <tbody>{b.attempts.map((a, i) => (
                        <tr key={i}><td>{a.prompt}</td><td>{a.answer}</td><td>{!a.scored ? "not scored" : a.correct ? <Badge tone="ok">correct</Badge> : <Badge tone="bad">incorrect</Badge>}{a.error_tags.length > 0 && <div className="small muted">{a.error_tags.join(", ").replace(/_/g, " ")}</div>}</td></tr>
                      ))}</tbody></table>)}
                </div>)}
              {e.sources && e.sources.length > 0 && (
                <div className="card"><h3>Course sources used</h3>
                  {e.sources.map((s) => <details key={s.chunk_id}><summary>{s.document_title}, page {s.page}</summary><blockquote className="small">{s.text}</blockquote></details>)}</div>)}
              <div className="card"><h3>Conversation with the student</h3>
                <Thread esc={e} me="teacher" canWrite={e.status === "ACCEPTED" && !!e.assigned_to_me} onSent={res.reload} /></div>
              {e.can_resolve && e.assigned_to_me && <ResolveForm e={e} onDone={() => { res.reload(); go("/"); }} />}
              {e.status === "RESOLVED" && e.resolution && <Notice kind="success"><b>Resolved.</b> {e.resolution.notes}</Notice>}
            </>
          );
        }}
      </Async>
    </Page>
  );
}

const toLocalInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

export function Availability() {
  const res = useLoad(() => api.availability(), []);
  const prof = useLoad(() => api.teacherProfile(), []);
  const [start, setStart] = useState(toLocalInput(new Date(Date.now() + 3600_000)));
  const [hours, setHours] = useState(2);
  const act = useAction();
  const save = (slots: { start_at: string; end_at: string }[], msg: string) => act.run(() => api.saveAvailability(slots), msg).then((r) => r && res.reload());
  return (
    <Page title="Availability" sub="Matching prefers teachers who are free soon. Times are shown in your local time zone.">
      <Async res={prof}>{(p) => <p className="muted small">Teaching: {p.topics.map((t) => `${t.name} (${t.proficiency})`).join(", ") || "no topics set"} · Languages: {p.languages.join(", ")}</p>}</Async>
      <Async res={res} empty={() => null}>
        {(d) => {
          const current = d.slots.filter((s) => new Date(s.end_at) > new Date()).map((s) => ({ start_at: s.start_at, end_at: s.end_at }));
          return (
            <>
              <div className="card"><h3>Your slots</h3>
                {d.slots.length === 0 ? <Empty>No slots yet. Add one below so you can be matched.</Empty> : (
                  <table className="t"><thead><tr><th>From</th><th>To</th><th /><th /></tr></thead>
                    <tbody>{d.slots.map((s, i) => (
                      <tr key={s.id}><td>{fmt(s.start_at)}</td><td>{fmt(s.end_at)}</td><td>{s.booked && <Badge tone="info">booked</Badge>}</td>
                        <td><button className="linkish" disabled={act.busy || s.booked} onClick={() => save(d.slots.filter((_, j) => j !== i).map((x) => ({ start_at: x.start_at, end_at: x.end_at })), "Slot removed.")}>Remove</button></td></tr>
                    ))}</tbody></table>)}
              </div>
              <form className="card" onSubmit={(e) => { e.preventDefault(); const s = new Date(start); save([...current, { start_at: s.toISOString(), end_at: new Date(s.getTime() + hours * 3600_000).toISOString() }], "Slot added."); }}>
                <h3>Add a slot</h3>
                <div className="grid cols-2">
                  <Field label="Starts"><input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} required /></Field>
                  <Field label="Length (hours, up to 12)"><input type="number" min={1} max={12} value={hours} onChange={(e) => setHours(Number(e.target.value))} /></Field>
                </div>
                {act.error && <Notice kind="error">{act.error}</Notice>}{act.done && <Notice kind="success">{act.done}</Notice>}
                <Btn type="submit" disabled={act.busy}>{act.busy ? "Saving…" : "Add slot"}</Btn>
              </form>
            </>
          );
        }}
      </Async>
    </Page>
  );
}
