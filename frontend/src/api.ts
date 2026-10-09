// The access token lives in module memory only (never localStorage/sessionStorage): a page reload logs out.
// All data comes from the backend; nothing here is hardcoded learner state.
let token: string | null = null;

export type Role = "student" | "teacher" | "admin";
export type User = { id: string; role: Role; display_name: string; email: string };
export type Citation = { n: number; chunk_id: string; document_id: string; document_title: string; page: number; quote: string; verified: boolean };
export type Intervention = {
  id: string; action: string; rule_id?: string; provider?: string; model?: string; fallback?: string | null;
  clarification_question?: string; message?: string; outcome?: string;
  explanation?: { text: string; citations: Citation[] };
  practice?: { available: boolean; reason?: string };
  escalation?: { id: string; status: string };
};
export type Attempt = {
  attempt_id: string; status: string; answer: string; scored: boolean; correct: boolean | null; partial_credit: number | null;
  feedback: string | null; error_tags: string[]; uncertainty: number | null; grader: string | null; counted_as_evidence: boolean;
  reveal?: { correct_answer: string; rubric: string | null };
};
export type PracticeItem = {
  item_id: string; position: number; kind: "mcq" | "numeric" | "short_text"; prompt: string; options: string[] | null;
  difficulty: string; targets_suspected_gap: boolean; attempt: Attempt | null;
};
export type EscalationInfo = { id: string; status: string; assigned_teacher: string | null; expires_at: string; outcome: string | null; waiting_for: string | null };
export type SessionView = {
  session_id: string; run_id: string | null; status: string; course_id: string; created_at: string;
  topic: { id: string; name: string } | null; latest_intervention: Intervention | null;
  practice: { set: number; items: PracticeItem[] }[]; escalation: EscalationInfo | null;
  messages: { id: string; role: string; content: string; created_at: string }[];
};
export type DoubtRow = { session_id: string; status: string; course_id: string; created_at: string; topic: string | null; text: string };

export type Evidence = { id: string; type: string; weight: number; at: string; attempt_id: string | null; escalation_id: string | null };
export type GapNode = {
  topic_id: string; slug: string; name: string; status: "not_assessed" | "suspected_gap" | "practising" | "improving" | "demonstrated";
  label: string; gap_kind: "suspected" | "confirmed" | null; reason: string; mean: number | null; evidence_count: number;
  last_evidence_at: string | null; evidence: Evidence[]; prerequisites: string[];
  check_prerequisites: { topic_id: string; name: string; status: string }[];
  hypotheses: { id: string; description: string; status: string; created_at: string }[];
  attempts?: { attempts: number; correct: number };
};
export type GapMap = { course_id: string; topics: GapNode[]; counts: Record<string, number>; statuses: string[]; note: string };
export type Passport = {
  student: { display_name: string }; course_id: string; topics: GapNode[]; counts: Record<string, number>; digest: string;
  doubts: { session_id: string; created_at: string; text: string; status: string; outcome: string | null; unresolved: boolean; last_action: string | null; last_rule: string | null }[];
  teacher_feedback: { escalation_id: string; at: string; notes: string; assessments: { topic_id: string; level: string }[] }[];
  history: { at: string; topic: string; status: string; mean: number; evidence_count: number; evidence: { id: string; type: string; weight: number } }[];
  totals: { doubts: number; unresolved: number; graded_attempts: number }; retention: string; note: string;
};
export type DecisionExplanation = {
  decision_id: string; at: string; rule_id: string; action: string; title: string; reasons: string[]; hard_rule: boolean; precedence: string;
  outcome: string | null; evidence: { id: string; type: string; weight: number; at: string }[]; provider: string | null; model: string | null;
  fallbacks: string[]; model_calls: { node: string; provider: string | null; model: string | null; prompt_version: string | null; fallback: boolean }[] | null;
  errors: { node: string; error: string }[] | null;
};
export type ThreadMsg = { id?: string; role: string; content: string; at?: string; created_at?: string; author?: string };
export type EscalationRow = {
  id: string; status: string; topic: string | null; reason_rule_id: string; created_at: string; expires_at: string;
  assigned_teacher_id: string | null; doubt: string; matched_candidates: number;
};
export type EscalationView = {
  id: string; status: string; session_id: string; course: { id: string; name: string }; topic: { id: string; name: string } | null;
  created_at: string; expires_at: string; resolved_at: string | null; messages: ThreadMsg[];
  assigned_teacher?: string | null; waiting_for?: string | null; can_message?: boolean; resolution?: { notes: string } | null; rated?: boolean | null; outcome?: string | null;
  // teacher / admin only
  student?: { display_name: string; language: string }; can_accept?: boolean; can_resolve?: boolean; assigned_to_me?: boolean;
  sources?: { chunk_id: string; document_title: string; page: number; text: string }[];
  doubt_conversation?: { role: string; content: string; at: string }[];
  live?: { mastery: { status: string; mean: number; evidence_count: number }; hypotheses: { id: string; description: string; status: string }[] } | null;
  brief?: {
    doubt: string; follow_ups: string[]; reason: { rule_id: string; reasons: string[] }; topic: { id: string; name: string } | null;
    explanation_given: boolean; last_explanation: string;
    attempts: { prompt: string; kind: string; difficulty: string; answer: string; correct: boolean | null; scored: boolean; error_tags: string[]; feedback: string }[];
    hypotheses: { id: string; description: string; status: string }[]; mastery: { status: string; mean: number; evidence_count: number } | null;
  };
  candidates?: { teacher_id: string; name: string; score: number; rank: number; components: Record<string, number> }[];
  events?: { event: string; at: string; meta: Record<string, unknown> }[];
  resume?: { status: string; outcome: string | null }; assigned_teacher_id?: string | null; reason_rule_id?: string;
};
export type Slot = { id: string; start_at: string; end_at: string; booked?: boolean };
export type TeacherProfile = { display_name: string; bio: string; languages: string[]; active: boolean; topics: { name: string; proficiency: number }[] };
export type Trace = {
  run: { run_id: string; session_id: string; status: string; student_ref: string; outcome: string | null; current_node: string };
  summary: { rules_fired: string[]; final_rule_id: string | null; final_action: string | null; providers_used: string[] };
  steps: { seq: number; node: string; latency_ms: number; provider: string | null; model: string | null; prompt_version: string | null; error: string | null; output: Record<string, unknown> }[];
  decisions: { decision_id: string; rule_id: string; action: string; reasons: string[]; evidence_refs: string[] }[];
  retrieval: { query: string; mode: string | null; results: { chunk_id: string; page: number; matched_terms: number }[] }[];
  citation_validation: { fallback: string | null; n_verified: number; n_stripped: number; checks: { chunk_id: string; quote: string; verified: boolean; reason: string }[] }[];
};
export type SystemInfo = {
  retrieval: { configured_mode: string; effective_mode: string; embedding_provider: string; pgvector?: boolean; fallback_reason?: string | null; embedding_model?: string; missing_embeddings?: number };
  llm: { provider: string; model?: string; reachable: boolean; error?: string };
  documents: Record<string, number>; ingestion_jobs: Record<string, number>; workflow_runs: Record<string, number>;
  escalations: Record<string, number>; pending_resumes: number;
};
export type IngestionJob = {
  job_id: string; title: string; document_status: string; kind: string; status: string; stage: string | null; attempts: number;
  max_attempts: number; error_code: string | null; last_error: string | null; created_at: string; finished_at: string | null;
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export const errText = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong");

const newKey = () => crypto.randomUUID();
let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

async function call<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const h: Record<string, string> = { ...headers };
  if (token) h["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) h["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "NETWORK", "Cannot reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let code = "ERROR", msg = res.statusText || "Request failed";
    try {
      const e = (await res.json()).error;
      code = e.code; msg = e.message;
      if (e.details?.errors?.length) msg += `: ${e.details.errors.map((x: { msg?: string }) => x.msg).filter(Boolean).join("; ")}`;
    } catch { /* non-JSON error body */ }
    if (res.status === 401 && token) { token = null; onUnauthorized(); }
    throw new ApiError(res.status, code, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}
// A write that may be retried by the user keeps ONE key per logical action so the server can de-duplicate it.
export const idemKey = newKey;

export const api = {
  async login(email: string, password: string): Promise<User> {
    const r = await call<{ access_token: string; user: User }>("POST", "/v1/auth/login", { email, password });
    token = r.access_token;
    return r.user;
  },
  logout() { token = null; },
  // student
  courses: () => call<{ items: { document_id: string; course_id: string; title: string; status: string }[] }>("GET", "/v1/documents"),
  doubts: () => call<{ items: DoubtRow[] }>("GET", "/v1/doubts"),
  ask: (course_id: string, text: string, key = newKey()) =>
    call<{ session_id: string; run_id: string; status: string }>("POST", "/v1/doubts", { course_id, text }, { "Idempotency-Key": key }),
  session: (sid: string) => call<SessionView>("GET", `/v1/doubts/${sid}`),
  reply: (sid: string, text: string) => call<{ status: string }>("POST", `/v1/doubts/${sid}/messages`, { text }),
  ack: (sid: string, ack: "understood" | "still_confused" | "check_me", key = newKey()) =>
    call<{ status: string; mastery_credit: number }>("POST", `/v1/doubts/${sid}/ack`, { ack }, { "Idempotency-Key": key }),
  requestTeacher: (sid: string) => call<{ status: string }>("POST", `/v1/doubts/${sid}/request-teacher`),
  answer: (sid: string, item_id: string, answer: string, key: string) =>
    call<Attempt & { run_status: string; remaining: number; mastery: { status: string; mean: number; evidence_count: number } }>(
      "POST", `/v1/doubts/${sid}/answers`, { item_id, answer, hints_used: 0 }, { "Idempotency-Key": key }),
  decisions: (sid: string) => call<{ items: DecisionExplanation[]; note: string }>("GET", `/v1/doubts/${sid}/decisions`),
  gapMap: () => call<GapMap>("GET", "/v1/learners/me/gap-map"),
  passport: () => call<Passport>("GET", "/v1/learners/me/passport"),
  // escalation (student + teacher + admin)
  escalation: (id: string) => call<EscalationView>("GET", `/v1/escalations/${id}`),
  postMessage: (id: string, content: string, key = newKey()) =>
    call<{ id: string }>("POST", `/v1/escalations/${id}/messages`, { content }, { "Idempotency-Key": key }),
  rate: (id: string, helpful: boolean) => call<{ helpful: boolean }>("POST", `/v1/escalations/${id}/rate`, { helpful }),
  // teacher
  teacherProfile: () => call<TeacherProfile>("GET", "/v1/teacher/profile"),
  inbox: (status?: string) => call<{ items: EscalationRow[] }>("GET", `/v1/teacher/escalations${status ? `?status=${status}` : ""}`),
  accept: (id: string) => call<{ status: string }>("POST", `/v1/teacher/escalations/${id}/accept`),
  release: (id: string) => call<{ status: string }>("POST", `/v1/teacher/escalations/${id}/release`),
  resolve: (id: string, body: { notes: string; topic_assessments: { topic_id: string; level: string }[]; hypothesis_decisions: { hypothesis_id: string; decision: string }[] }, key: string) =>
    call<{ status: string; workflow_resumed: boolean }>("POST", `/v1/teacher/escalations/${id}/resolve`, body, { "Idempotency-Key": key }),
  availability: () => call<{ slots: Slot[] }>("GET", "/v1/teacher/availability"),
  saveAvailability: (slots: { start_at: string; end_at: string }[]) =>
    call<{ slots: Slot[] }>("PUT", "/v1/teacher/availability", { slots }, { "Idempotency-Key": newKey() }),
  // admin
  runs: () => call<{ items: { run_id: string; session_id: string; status: string; student_ref: string; created_at: string }[] }>("GET", "/v1/admin/runs"),
  trace: (rid: string) => call<Trace>("GET", `/v1/admin/runs/${rid}/trace`),
  system: () => call<SystemInfo>("GET", "/v1/admin/system"),
  ingestion: () => call<{ items: IngestionJob[] }>("GET", "/v1/admin/ingestion/jobs"),
  adminEscalations: (status?: string) => call<{ items: EscalationRow[] }>("GET", `/v1/admin/escalations${status ? `?status=${status}` : ""}`),
  adminTeachers: () => call<{ items: { id: string; display_name: string; languages: string[]; active: boolean; open_load: number }[] }>("GET", "/v1/admin/teachers"),
  assign: (id: string, teacher_id: string) =>
    call<{ status: string }>("POST", `/v1/admin/escalations/${id}/assign`, { teacher_id }, { "Idempotency-Key": newKey() }),
  reconcile: () => call<{ expired: number; pending: number; resumed: number }>("POST", "/v1/admin/escalations/reconcile"),
};
