import { FormEvent, useRef, useState } from "react";
import { api, Citation, idemKey, PracticeItem, SessionView } from "../api";
import { Thread } from "../Thread";
import { Async, Badge, Btn, Empty, fmt, Notice, Page, Spinner, StatusBadge, useAction, useLoad } from "../ui";

const STAGES = ["Understand", "Explain", "Practise", "Verify", "Resolved"];

function stage(v: SessionView): number {
  if (v.status === "COMPLETED") return 4;
  if (v.practice.some((s) => s.items.some((i) => i.attempt?.scored))) return 3;
  if (v.practice.length) return 2;
  if (v.latest_intervention?.explanation) return 1;
  return 0;
}

function Explanation({ v }: { v: SessionView }) {
  const li = v.latest_intervention;
  const [open, setOpen] = useState<number | null>(null);
  const refs = useRef<Record<number, HTMLDetailsElement | null>>({});
  if (!li?.explanation) return null;
  const cites: Citation[] = li.explanation.citations;
  const parts = li.explanation.text.split(/(\[\d+\])/g);
  return (
    <div className="card">
      <div className="row between"><h3>Explanation from your course material</h3>
        {li.provider && <Badge tone={li.provider === "fake" ? "warn" : "info"}>{li.provider === "fake" ? "Demo mode (no language model)" : `${li.provider}${li.model ? " · " + li.model : ""}`}</Badge>}</div>
      <p className="explain">{parts.map((p, i) => {
        const m = /^\[(\d+)\]$/.exec(p);
        if (!m) return <span key={i}>{p}</span>;
        const n = Number(m[1]);
        return <button key={i} className="cite-btn" aria-label={`Show source ${n}`} onClick={() => { setOpen(n); const el = refs.current[n]; if (el) { el.open = true; el.scrollIntoView({ block: "nearest" }); } }}>{p}</button>;
      })}</p>
      {li.fallback && <Notice kind="warn">Note: {li.fallback.replace(/_/g, " ")}.</Notice>}
      {cites.length > 0
        ? <ol className="cites" aria-label="Sources">{cites.map((c) => (
            <li key={c.chunk_id + c.n}>
              <details ref={(el) => { refs.current[c.n] = el; }} open={open === c.n}>
                <summary>[{c.n}] {c.document_title}, page {c.page} {c.verified && <Badge tone="ok">verified in source</Badge>}</summary>
                <blockquote>{c.quote}</blockquote>
              </details>
            </li>))}</ol>
        : <p className="muted small">No citations: the answer was limited to what the sources support.</p>}
    </div>
  );
}

function PracticeCard({ sid, item, index, onDone }: { sid: string; item: PracticeItem; index: number; onDone: () => void }) {
  const [val, setVal] = useState("");
  const [key, setKey] = useState(idemKey);          // one key per question: a retry of the same submission cannot double-count
  const act = useAction();
  const a = item.attempt;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.answer(sid, item.item_id, val, key));
    if (r) { onDone(); setKey(idemKey()); }          // on failure the key is kept, so a retry of the same submission stays idempotent
  };
  const tone = !a?.scored ? "" : a.status === "UNCERTAIN" ? "unsure" : a.correct ? "right" : "wrong";
  return (
    <div className={`q ${tone}`}>
      <div className="row between"><b>Question {index + 1}</b>
        <span className="row"><Badge>{item.kind === "mcq" ? "multiple choice" : item.kind === "numeric" ? "number" : "short answer"}</Badge><Badge>{item.difficulty}</Badge>
          {item.targets_suspected_gap && <Badge tone="warn">checks a suspected gap</Badge>}</span></div>
      <p>{item.prompt}</p>
      {a?.scored ? (
        <div>
          <p className="small muted">Your answer: <b>{a.answer}</b></p>
          <p>{a.status === "UNCERTAIN" ? <Badge tone="warn">Needs review: not counted as proof</Badge> : a.correct ? <Badge tone="ok">Correct</Badge> : <Badge tone="bad">Not yet</Badge>}{" "}
            {!a.counted_as_evidence && <Badge>not counted as evidence</Badge>}</p>
          {a.feedback && <p>{a.feedback}</p>}
          {a.reveal && !a.correct && <p className="small"><b>Reference answer:</b> {a.reveal.correct_answer}</p>}
          {a.error_tags.length > 0 && <p className="small muted">Mistake pattern: {a.error_tags.join(", ").replace(/_/g, " ")}</p>}
        </div>
      ) : (
        <form onSubmit={submit}>
          {item.kind === "mcq" && item.options?.map((o) => (
            <label key={o} className="opt"><input type="radio" name={item.item_id} value={o} checked={val === o} onChange={() => setVal(o)} /><span>{o}</span></label>
          ))}
          {item.kind === "numeric" && <label className="field"><span>Your answer (a number)</span><input inputMode="decimal" value={val} onChange={(e) => setVal(e.target.value)} /></label>}
          {item.kind === "short_text" && <label className="field"><span>Your answer</span><textarea value={val} onChange={(e) => setVal(e.target.value)} maxLength={2000} /></label>}
          {act.error && <Notice kind="error">{act.error}</Notice>}
          <Btn type="submit" disabled={act.busy || !val.trim()}>{act.busy ? "Checking…" : "Submit answer"}</Btn>
        </form>
      )}
    </div>
  );
}

function Why({ sid }: { sid: string }) {
  const res = useLoad(() => api.decisions(sid), [sid]);
  return (
    <details className="why">
      <summary>Why did the system choose this step?</summary>
      <Async res={res} empty={(d) => (d.items.length ? null : <p className="muted">No decisions recorded yet.</p>)}>
        {(d) => (
          <div className="stack">
            {d.items.map((x) => (
              <div key={x.decision_id}>
                <div className="row"><b>{x.title}</b><Badge tone={x.hard_rule ? "bad" : "accent"}>{x.rule_id.split("_")[0]}{x.hard_rule ? " · hard rule" : ""}</Badge></div>
                <ul className="small">{x.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
                <p className="small muted">{x.precedence}</p>
                {x.evidence.length > 0 && <p className="small">Evidence used: {x.evidence.map((e) => `${e.type.replace(/_/g, " ")} (weight ${e.weight})`).join(", ")}</p>}
                {x.fallbacks.length > 0 && <p className="small">Fallback used in: {x.fallbacks.join(", ")}</p>}
              </div>
            ))}
            <p className="small muted">{d.note}</p>
          </div>
        )}
      </Async>
    </details>
  );
}

function Escalation({ v, reload }: { v: SessionView; reload: () => void }) {
  const info = v.escalation!;
  const res = useLoad(() => api.escalation(info.id), [info.id, info.status], info.status === "OPEN" || info.status === "ACCEPTED" ? 15000 : 0);
  const act = useAction();
  return (
    <div className="card">
      <div className="row between"><h3>Teacher help</h3><StatusBadge status={info.status} /></div>
      <Async res={res}>
        {(e) => (
          <div className="stack">
            {e.status === "OPEN" && <p>Your request is waiting for a suitable teacher. It stays open until <b>{fmt(e.expires_at)}</b>; nothing is lost if no one is free yet.</p>}
            {e.status === "ACCEPTED" && <p><b>{e.assigned_teacher}</b> has your case and will reply here.</p>}
            {e.status === "RESOLVED" && e.resolution && <Notice kind="success"><b>Resolved.</b> {e.resolution.notes}</Notice>}
            {e.status === "EXPIRED" && <Notice kind="warn">No teacher replied in time, so this doubt is marked unresolved. You can ask again.</Notice>}
            <Thread esc={e} me="student" canWrite={!!e.can_message} onSent={() => { res.reload(); }} />
            {e.status === "RESOLVED" && e.rated == null && (
              <div className="row"><span>Was this helpful?</span>
                <Btn small kind="secondary" disabled={act.busy} onClick={() => act.run(() => api.rate(e.id, true), "Thanks for the feedback.").then(res.reload)}>Yes</Btn>
                <Btn small kind="ghost" disabled={act.busy} onClick={() => act.run(() => api.rate(e.id, false), "Thanks for the feedback.").then(res.reload)}>No</Btn></div>)}
            {act.done && <Notice kind="success">{act.done}</Notice>}
            {act.error && <Notice kind="error">{act.error}</Notice>}
            <Btn small kind="ghost" onClick={() => { res.reload(); reload(); }}>Refresh status</Btn>
          </div>
        )}
      </Async>
    </div>
  );
}

export function Doubt({ sid }: { sid: string }) {
  const res = useLoad(() => api.session(sid), [sid], 0);
  const act = useAction();
  const [reply, setReply] = useState("");
  const [ackKey, setAckKey] = useState(idemKey);
  return (
    <Page title="Your doubt" sub="Explain, practise, then verify. Understanding is shown by your work, not by clicking “I understood”."
      actions={<a className="btn ghost" href="#/">All doubts</a>}>
      <Async res={res}>
        {(v) => {
          const li = v.latest_intervention;
          const after = async (fn: () => Promise<unknown>) => { await act.run(fn); res.reload(); };
          const practiceNow = v.status === "AWAITING_ANSWER";
          const setsNow = v.practice[v.practice.length - 1];
          return (
            <>
              <ol className="steps" aria-label="Progress of this doubt">
                {STAGES.map((s, i) => <li key={s} className={i < stage(v) ? "done" : i === stage(v) ? "now" : ""} aria-current={i === stage(v) ? "step" : undefined}>{s}</li>)}
              </ol>
              <div className="row" style={{ marginBottom: 12 }}><StatusBadge status={v.status} />{v.topic && <Badge tone="accent">{v.topic.name}</Badge>}<span className="muted small">{fmt(v.created_at)}</span></div>
              <div className="card flat"><b>You asked:</b> {v.messages[0]?.content}</div>
              {act.error && <Notice kind="error">{act.error}</Notice>}

              {li?.clarification_question && v.status === "AWAITING_STUDENT" && (
                <form className="card" onSubmit={async (e) => { e.preventDefault(); await after(() => api.reply(sid, reply)); setReply(""); }}>
                  <h3>A quick question first</h3><p>{li.clarification_question}</p>
                  <label className="field"><span className="sr-only">Your answer</span><input value={reply} onChange={(e) => setReply(e.target.value)} maxLength={2000} placeholder="Your answer" /></label>
                  <Btn type="submit" disabled={act.busy || !reply.trim()}>Reply</Btn>
                </form>
              )}

              <Explanation v={v} />

              {li?.explanation && v.status === "AWAITING_STUDENT" && (
                <div className="card" role="group" aria-label="What next?">
                  <h3>What would you like to do next?</h3>
                  <p className="muted small">Your choice is recorded, but it never counts as mastery. Only graded practice does.</p>
                  <div className="row">
                    <Btn disabled={act.busy} onClick={() => after(async () => { await api.ack(sid, "check_me", ackKey); setAckKey(idemKey()); })}>Check me with practice</Btn>
                    <Btn kind="secondary" disabled={act.busy} onClick={() => after(async () => { await api.ack(sid, "understood", ackKey); setAckKey(idemKey()); })}>I understood</Btn>
                    <Btn kind="ghost" disabled={act.busy} onClick={() => after(async () => { await api.ack(sid, "still_confused", ackKey); setAckKey(idemKey()); })}>Still confused</Btn>
                    <Btn kind="ghost" disabled={act.busy} onClick={() => after(() => api.requestTeacher(sid))}>Ask a teacher</Btn>
                  </div>
                  {act.busy && <Spinner label="Working" />}
                </div>
              )}
              {li?.practice && !li.practice.available && <Notice kind="warn"><b>Practice is not available for this doubt.</b> {li.message ?? li.practice.reason}</Notice>}

              {v.practice.length > 0 && (
                <div className="card">
                  <h3>Practice</h3>
                  {practiceNow && <p className="muted small">Answer each question. Correct answers are shown after you submit.</p>}
                  {v.practice.map((s) => (
                    <div key={s.set}>{v.practice.length > 1 && <h4>Set {s.set + 1}</h4>}
                      {s.items.map((it, i) => <PracticeCard key={it.item_id} sid={sid} item={it} index={i} onDone={res.reload} />)}</div>
                  ))}
                  {!practiceNow && setsNow && setsNow.items.every((i) => i.attempt?.scored) && v.status === "AWAITING_STUDENT" && <p className="muted small">Set complete. See what comes next above.</p>}
                </div>
              )}

              {li?.outcome && (
                <Notice kind={li.outcome === "RESOLVED" ? "success" : "warn"}>
                  <b>{li.outcome === "RESOLVED" ? "Resolved" : li.outcome === "UNVERIFIED" ? "Not verified" : "Unresolved"}.</b> {li.message}
                </Notice>
              )}
              {v.escalation && <Escalation v={v} reload={res.reload} />}
              {v.status === "AWAITING_STUDENT" && !v.escalation && !li?.explanation && (
                <div className="row"><Btn kind="ghost" disabled={act.busy} onClick={() => after(() => api.requestTeacher(sid))}>Ask a teacher</Btn></div>
              )}
              {v.run_id && <Why sid={sid} />}
            </>
          );
        }}
      </Async>
    </Page>
  );
}

export function Ask({ onCreated }: { onCreated: (sid: string) => void }) {
  const [text, setText] = useState("");
  const [key, setKey] = useState(idemKey);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(async () => {
      const courses = await api.courses();
      const cid = courses.items[0]?.course_id;
      if (!cid) throw new Error("You are not enrolled in a course with material yet.");
      return api.ask(cid, text.trim(), key);
    });
    if (r) { setKey(idemKey()); onCreated(r.session_id); }
  };
  return (
    <form className="card" onSubmit={submit}>
      <h2>What are you stuck on?</h2>
      <label className="field"><span className="sr-only">Your doubt</span>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={2000}
          placeholder="e.g. Why does TCP slow start stop doubling the congestion window?" /></label>
      {act.error && <Notice kind="error">{act.error}</Notice>}
      <Btn type="submit" disabled={act.busy || text.trim().length < 3}>{act.busy ? "Finding sources and explaining…" : "Ask"}</Btn>
      <span className="muted small"> Answers use only your course material.</span>
    </form>
  );
}

export function DoubtList({ onOpen }: { onOpen: (sid: string) => void }) {
  const res = useLoad(() => api.doubts(), []);
  return (
    <Async res={res} empty={(d) => (d.items.length ? null : <Empty>No doubts yet. Ask your first question above.</Empty>)}>
      {(d) => (
        <div className="stack">
          {d.items.map((x) => (
            <button key={x.session_id} className="card node" style={{ width: "100%", borderLeftWidth: 1 }} onClick={() => onOpen(x.session_id)}>
              <div className="row between"><b>{x.text || "(no text)"}</b><StatusBadge status={x.status} /></div>
              <span className="muted small">{x.topic ?? "Topic not identified"} · {fmt(x.created_at)}</span>
            </button>
          ))}
        </div>
      )}
    </Async>
  );
}
