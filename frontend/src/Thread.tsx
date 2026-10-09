import { FormEvent, useState } from "react";
import { api, EscalationView, idemKey } from "./api";
import { Btn, fmt, Notice, useAction } from "./ui";

/** The asynchronous student <-> teacher conversation. One Idempotency-Key per draft, so a retry never posts twice. */
export function Thread({ esc, me, canWrite, onSent }: { esc: EscalationView; me: "student" | "teacher"; canWrite: boolean; onSent: () => void }) {
  const [text, setText] = useState("");
  const [key, setKey] = useState(idemKey);
  const act = useAction();
  const send = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.postMessage(esc.id, text.trim(), key));
    if (r) { setText(""); setKey(idemKey()); onSent(); }
  };
  return (
    <div>
      {esc.messages.length === 0
        ? <p className="muted small">No messages yet. {me === "student" ? "You can add context while you wait; the teacher will see it." : "Accept the case to start the conversation."}</p>
        : <div className="thread" aria-label="Conversation" role="log">
            {esc.messages.map((m) => (
              <div key={m.id} className={`bubble ${m.role === me ? "me" : ""}`}>
                <small>{m.author ?? m.role} · {fmt(m.at)}</small>{m.content}
              </div>
            ))}
          </div>}
      {canWrite
        ? <form onSubmit={send}>
            <label className="field"><span className="sr-only">Message</span>
              <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={4000} rows={3}
                placeholder={me === "student" ? "Add detail for your teacher…" : "Reply to the student…"} />
            </label>
            {act.error && <Notice kind="error">{act.error}</Notice>}
            <Btn type="submit" disabled={act.busy || !text.trim()}>{act.busy ? "Sending…" : "Send message"}</Btn>
          </form>
        : <p className="muted small">This conversation is closed.</p>}
    </div>
  );
}
