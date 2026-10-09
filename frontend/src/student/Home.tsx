import { api, User } from "../api";
import { Async, Badge, Notice, StatusBadge, useLoad } from "../ui";
import { Ask, DoubtList } from "./Doubt";

const NEXT: Record<string, string> = {
  AWAITING_STUDENT: "Waiting for your reply", AWAITING_ANSWER: "Practice questions are ready for you", WAITING_HUMAN: "A teacher is on it",
};

export function Home({ user, go }: { user: User; go: (to: string) => void }) {
  const doubts = useLoad(() => api.doubts(), []);
  const map = useLoad(() => api.gapMap(), []);
  const first = user.display_name.split(" ")[0];
  return (
    <>
      <div className="hero">
        <h2>Welcome back, {first}</h2>
        <p className="muted">Ask anything from your course. You will get an explanation with sources, then a short check that shows what has really clicked.</p>
      </div>
      <Ask onCreated={(sid) => go(`/doubt/${sid}`)} />

      <div className="grid cols-2">
        <div className="card">
          <h3>Needs your attention</h3>
          <Async res={doubts}>
            {(d) => {
              const active = d.items.filter((x) => NEXT[x.status]);
              return active.length === 0
                ? <p className="muted">Nothing is waiting on you. Ask a new question or review your progress.</p>
                : <div className="stack">{active.map((x) => (
                    <a key={x.session_id} href={`#/doubt/${x.session_id}`} style={{ display: "block", textDecoration: "none", color: "inherit" }}>
                      <div className="row between"><b>{x.text.slice(0, 70) || "Doubt"}</b><StatusBadge status={x.status} /></div>
                      <span className="muted small">{NEXT[x.status]}</span>
                    </a>))}</div>;
            }}
          </Async>
        </div>
        <div className="card">
          <div className="row between"><h3>Your progress</h3><a href="#/gap-map">Gap Map</a></div>
          <Async res={map}>
            {(g) => {
              const need = g.topics.filter((t) => t.status === "suspected_gap" || t.status === "practising");
              const done = g.topics.filter((t) => t.status === "demonstrated").length;
              return (
                <>
                  <p><Badge tone="ok">{done} demonstrated</Badge> <Badge tone="warn">{need.length} to work on</Badge> <Badge>{g.counts.not_assessed ?? 0} not assessed</Badge></p>
                  {need.length === 0
                    ? <p className="muted small">{g.topics.every((t) => t.evidence_count === 0) ? "No graded practice yet. Progress appears after you answer practice questions." : "No open gaps right now."}</p>
                    : <ul className="small">{need.map((t) => <li key={t.slug}><a href="#/gap-map">{t.name}</a>: {t.label.toLowerCase()}</li>)}</ul>}
                </>
              );
            }}
          </Async>
        </div>
      </div>

      <h3>Recent doubts</h3>
      <DoubtList onOpen={(sid) => go(`/doubt/${sid}`)} />
      {doubts.error && <Notice kind="error">{doubts.error}</Notice>}
    </>
  );
}
