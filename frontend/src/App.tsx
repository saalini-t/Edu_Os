import { FormEvent, useEffect, useState } from "react";
import { api, setUnauthorizedHandler, User } from "./api";
import { AdminEscalations, Ingestion, Overview, Runs } from "./admin/Admin";
import { Documents } from "./Documents";
import { Doubt } from "./student/Doubt";
import { Home } from "./student/Home";
import { GapMapView, PassportView } from "./student/Progress";
import { Availability, Case, Inbox } from "./teacher/Teacher";
import { Btn, Empty, Field, Notice, useAction, useRoute } from "./ui";

function Login({ onUser }: { onUser: (u: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const u = await act.run(() => api.login(email.trim(), password));
    if (u) onUser(u);
  };
  return (
    <main className="login">
      <div className="brand"><i />EduOS</div>
      <form className="card" onSubmit={submit}>
        <h1>Sign in</h1>
        <p className="muted">Your AI learning workspace.</p>
        <Field label="Email"><input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required /></Field>
        <Field label="Password"><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></Field>
        {act.error && <Notice kind="error">{act.error}</Notice>}
        <Btn type="submit" disabled={act.busy || !email || !password}>{act.busy ? "Signing in…" : "Sign in"}</Btn>
        <p className="muted small">Demo accounts are listed in the README.</p>
      </form>
    </main>
  );
}

type NavItem = { to: string; label: string; match: (r: string) => boolean };
const NAV: Record<User["role"], NavItem[]> = {
  student: [
    { to: "/", label: "Home", match: (r) => r === "/" || r.startsWith("/doubt") },
    { to: "/documents", label: "Documents", match: (r) => r === "/documents" },
    { to: "/gap-map", label: "Gap Map", match: (r) => r === "/gap-map" },
    { to: "/passport", label: "Learning Passport", match: (r) => r === "/passport" },
  ],
  teacher: [
    { to: "/", label: "Case inbox", match: (r) => r === "/" || r.startsWith("/case") },
    { to: "/availability", label: "Availability", match: (r) => r === "/availability" },
  ],
  admin: [
    { to: "/", label: "System health", match: (r) => r === "/" },
    { to: "/runs", label: "Workflow runs", match: (r) => r === "/runs" },
    { to: "/escalations", label: "Escalations", match: (r) => r === "/escalations" },
    { to: "/documents", label: "Documents", match: (r) => r === "/documents" },
    { to: "/ingestion", label: "Ingestion", match: (r) => r === "/ingestion" },
  ],
};

function Routes({ user, route, go }: { user: User; route: string; go: (to: string) => void }) {
  const part = route.split("/");
  if (user.role === "student") {
    if (route === "/") return <Home user={user} go={go} />;
    if (part[1] === "doubt" && part[2]) return <Doubt sid={part[2]} />;
    if (route === "/documents") return <Documents />;
    if (route === "/gap-map") return <GapMapView />;
    if (route === "/passport") return <PassportView />;
  }
  if (user.role === "teacher") {
    if (route === "/") return <Inbox />;
    if (part[1] === "case" && part[2]) return <Case id={part[2]} go={go} />;
    if (route === "/availability") return <Availability />;
  }
  if (user.role === "admin") {
    if (route === "/") return <Overview />;
    if (route === "/runs") return <Runs />;
    if (route === "/documents") return <Documents />;
    if (route === "/escalations") return <AdminEscalations />;
    if (route === "/ingestion") return <Ingestion />;
  }
  return <Empty action={<a href="#/">Go to start</a>}>That page does not exist for your role.</Empty>;
}

import { Component, ReactNode } from "react";

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? <main className="login"><Notice kind="error"><b>Something went wrong showing this page.</b> <button className="linkish" onClick={() => { window.location.hash = "#/"; window.location.reload(); }}>Reload</button></Notice></main>
      : this.props.children;
  }
}

export default function App() { return <Boundary><AppInner /></Boundary>; }

function AppInner() {
  const [user, setUser] = useState<User | null>(null);
  const [route, go] = useRoute();
  const [expired, setExpired] = useState(false);
  useEffect(() => { setUnauthorizedHandler(() => { setUser(null); setExpired(true); }); }, []);
  if (!user) {
    return <>{expired && <div className="login"><Notice kind="warn">Your session ended. Please sign in again.</Notice></div>}<Login onUser={(u) => { setExpired(false); go("/"); setUser(u); }} /></>;
  }
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><i />EduOS</div>
        <nav className="nav" aria-label="Main">
          {NAV[user.role].map((n) => <a key={n.to} href={`#${n.to}`} aria-current={n.match(route) ? "page" : undefined}>{n.label}</a>)}
        </nav>
        <div className="who"><b>{user.display_name}</b><span>{user.role}</span>
          <Btn kind="ghost" small onClick={() => { api.logout(); setUser(null); go("/"); }}>Sign out</Btn></div>
      </aside>
      <main className="content"><Routes user={user} route={route} go={go} /></main>
    </div>
  );
}
