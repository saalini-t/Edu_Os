import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { errText } from "./api";

// ---------------------------------------------------------------- data loading with explicit loading / error / empty states
export type Loaded<T> = { data: T | null; loading: boolean; error: string | null; reload: () => void; setData: (d: T) => void };

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const first = useRef(true);
  useEffect(() => {
    let alive = true;
    if (first.current) setLoading(true);
    fn().then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e) => { if (alive) setError(errText(e)); })
      .finally(() => { if (alive) { setLoading(false); first.current = false; } });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(() => setTick((x) => x + 1), pollMs);
    return () => clearInterval(t);
  }, [pollMs]);
  const reload = useCallback(() => setTick((x) => x + 1), []);
  return { data, loading, error, reload, setData };
}

// a user-triggered write with busy / error / success state
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>, success?: string): Promise<T | undefined> => {
    setBusy(true); setError(null); setDone(null);
    try { const r = await fn(); if (success) setDone(success); return r; }
    catch (e) { setError(errText(e)); return undefined; }
    finally { setBusy(false); }
  }, []);
  return { busy, error, done, run, clear: () => { setError(null); setDone(null); } };
}

// ---------------------------------------------------------------- tiny hash router (no dependency)
export function useRoute(): [string, (to: string) => void] {
  const get = () => window.location.hash.replace(/^#/, "") || "/";
  const [route, setRoute] = useState(get);
  useEffect(() => {
    const on = () => setRoute(get());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [route, (to) => { window.location.hash = to; }];
}

// ---------------------------------------------------------------- presentational pieces
export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span role="status" aria-live="polite"><span className="spinner" aria-hidden="true" /> <span className="muted">{label}…</span></span>;
}
export function Notice({ kind = "info", children }: { kind?: "info" | "error" | "success" | "warn"; children: ReactNode }) {
  return <div className={`notice ${kind === "info" ? "" : kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}
export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return <div className="empty"><p>{children}</p>{action}</div>;
}
export function Badge({ tone = "", children }: { tone?: "ok" | "warn" | "bad" | "info" | "accent" | ""; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Btn({ kind = "", small, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "secondary" | "ghost" | "danger" | ""; small?: boolean }) {
  return <button type="button" {...p} className={`btn ${kind} ${small ? "small" : ""} ${p.className ?? ""}`} />;
}
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="field"><span>{label}</span>{children}{hint && <span className="muted small">{hint}</span>}</label>;
}
export function Page({ title, sub, actions, children }: { title: string; sub?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="page-head"><div><h1>{title}</h1>{sub && <p>{sub}</p>}</div>{actions}</div>
      {children}
    </section>
  );
}
/** Renders a loaded resource: spinner first, then an error with Retry, then (optionally) the empty state, else the content. */
export function Async<T>({ res, empty, children }: { res: Loaded<T>; empty?: (d: T) => ReactNode | null; children: (d: T) => ReactNode }) {
  if (res.loading && !res.data) return <Spinner />;
  if (res.error && !res.data) return <Notice kind="error"><b>Could not load.</b> {res.error} <button className="linkish" onClick={res.reload}>Try again</button></Notice>;
  if (!res.data) return null;
  const e = empty?.(res.data);
  return <>{res.error && <Notice kind="warn">Showing the last data. Refresh failed: {res.error}</Notice>}{e ?? children(res.data)}</>;
}
export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { id: T; label: string }[] }) {
  return (
    <div className="tabs" role="tablist">
      {items.map((i) => <button key={i.id} role="tab" aria-selected={value === i.id} onClick={() => onChange(i.id)}>{i.label}</button>)}
    </div>
  );
}

export const fmt = (iso?: string | null) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");
export const pct = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x * 100)}%`);

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "info" | "accent" | ""> = {
  not_assessed: "", suspected_gap: "bad", practising: "warn", improving: "info", demonstrated: "ok",
  OPEN: "warn", ACCEPTED: "info", RESOLVED: "ok", EXPIRED: "bad", CANCELLED: "", WAITING_HUMAN: "warn", COMPLETED: "ok",
  AWAITING_STUDENT: "accent", AWAITING_ANSWER: "accent", FAILED: "bad", RUNNING: "info", DONE: "ok", SUCCEEDED: "ok", QUEUED: "info",
};
const STATUS_TEXT: Record<string, string> = {
  not_assessed: "Not assessed", suspected_gap: "Suspected gap", practising: "Practising", improving: "Improving", demonstrated: "Understanding demonstrated",
  AWAITING_STUDENT: "Waiting for you", AWAITING_ANSWER: "Practice ready", WAITING_HUMAN: "With a teacher", COMPLETED: "Completed",
  OPEN: "Waiting for a teacher", ACCEPTED: "Teacher assigned", RESOLVED: "Resolved", EXPIRED: "Expired",
};
export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? ""}>{STATUS_TEXT[status] ?? status.replace(/_/g, " ").toLowerCase()}</Badge>;
}
