import { useEffect, useRef, useState } from "react";
import { api, followRun, login, type Me, type Run, type RunEvent } from "./api";
import { AnswerCard } from "./components/Answer";
import { Approvals } from "./components/Approvals";
import { Timeline } from "./components/Timeline";

const EXAMPLES = [
  "What caused last week's payments outage?",
  "Which enterprise customers were hit by last week's payments outage, and what is their total ARR?",
  "Summarize INC-2042 and post the summary to #incident-review.",
  "How many customers churned yesterday?",
];

function Login({ onToken }: { onToken: (t: string) => void }) {
  const [username, setUsername] = useState("alice");
  const [password, setPassword] = useState("alice");
  const [error, setError] = useState("");
  return (
    <div className="login card">
      <h1>MARA</h1>
      <p className="muted">Multi-Agent Research &amp; Action platform</p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          try {
            onToken(await login(username, password));
          } catch (err: any) {
            setError(err.message);
          }
        }}
      >
        <label>User<input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <button type="submit">Sign in</button>
        {error && <p className="error">{error}</p>}
      </form>
      <p className="muted small">Demo users: alice (asks), bob (approver), dana (finance, EMEA). Password = user name.</p>
    </div>
  );
}

function Ask({ token }: { token: string }) {
  const [question, setQuestion] = useState("");
  const [allowActions, setAllowActions] = useState(true);
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);

  useEffect(() => () => abort.current?.abort(), []);

  async function submit(q: string) {
    abort.current?.abort();
    setEvents([]);
    setRun(null);
    setError("");
    try {
      const { run_id } = await api.ask(token, q, allowActions);
      setRunId(run_id);
      const controller = new AbortController();
      abort.current = controller;
      followRun(token, run_id, async (e) => {
        setEvents((prev) => [...prev, e]);
        if (["final", "approval_required", "action_executed", "action_failed", "run_finished"].includes(e.type)) {
          setRun(await api.run(token, run_id));
        }
      }, controller.signal);
    } catch (e: any) {
      setError(e.message);
    }
  }

  const waiting = run?.status === "awaiting_approval";
  return (
    <div className="page">
      <form className="ask" onSubmit={(e) => { e.preventDefault(); if (question.trim()) submit(question.trim()); }}>
        <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={3}
                  placeholder="Ask about incidents, customers, revenue, runbooks... or ask it to file a follow-up." />
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={allowActions} onChange={(e) => setAllowActions(e.target.checked)} />
            allow write actions (always need approval)
          </label>
          <button type="submit">Ask</button>
        </div>
        <div className="examples">
          {EXAMPLES.map((ex) => <button type="button" key={ex} className="chip" onClick={() => { setQuestion(ex); submit(ex); }}>{ex}</button>)}
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {runId && <p className="muted small">run {runId} · {run?.status ?? "running"}
        {run && ["running", "queued"].includes(run.status) && <button className="link" onClick={() => api.cancel(token, runId)}>cancel</button>}
      </p>}
      <div className="grid">
        <div>
          {run?.final && <AnswerCard final={run.final} />}
          {(waiting || (run?.actions?.length ?? 0) > 0) && (
            <section className="card">
              <h3>Actions</h3>
              {waiting && <p className="muted">An approver (bob) must approve these in the Approvals tab.</p>}
              {run!.actions.map((a) => (
                <div key={a.id} className="action-row">
                  <span className={`pill status-${a.status}`}>{a.status.replace(/_/g, " ")}</span> {a.tool}
                  {a.policy_reason && <span className="muted"> · {a.policy_reason}</span>}
                  <pre className="preview">{a.preview}</pre>
                </div>
              ))}
            </section>
          )}
        </div>
        <Timeline events={events} />
      </div>
    </div>
  );
}

function History({ token }: { token: string }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [open, setOpen] = useState<Run | null>(null);
  useEffect(() => { api.runs(token).then((r) => setRuns(r.runs)).catch(() => {}); }, [token]);
  return (
    <div className="page">
      <h2>Runs</h2>
      <table>
        <thead><tr><th>When</th><th>Question</th><th>Status</th><th>Cost</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} onClick={async () => setOpen(await api.run(token, r.id))}>
              <td>{new Date(r.created_at).toLocaleString()}</td><td>{r.question}</td>
              <td><span className={`pill status-${r.status}`}>{r.status.replace(/_/g, " ")}</span></td>
              <td>${Number(r.cost_usd).toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {open?.final && <AnswerCard final={open.final} />}
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem("mara_token"));
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<"ask" | "approvals" | "runs">("ask");

  useEffect(() => {
    if (!token) return;
    sessionStorage.setItem("mara_token", token);
    api.me(token).then(setMe).catch(() => { sessionStorage.removeItem("mara_token"); setToken(null); });
  }, [token]);

  if (!token || !me) return <Login onToken={setToken} />;
  const approver = me.roles.includes("approver");
  return (
    <div className="app">
      <nav>
        <strong className="brand">MARA</strong>
        <button className={tab === "ask" ? "active" : ""} onClick={() => setTab("ask")}>Ask</button>
        {approver && <button className={tab === "approvals" ? "active" : ""} onClick={() => setTab("approvals")}>Approvals</button>}
        <button className={tab === "runs" ? "active" : ""} onClick={() => setTab("runs")}>Runs</button>
        <span className="spacer" />
        <span className="muted small">{me.name ?? me.username} · {me.roles.join(", ")} · {me.groups.join(", ")}</span>
        <button className="link" onClick={() => { sessionStorage.removeItem("mara_token"); setToken(null); setMe(null); }}>sign out</button>
      </nav>
      {tab === "ask" && <Ask token={token} />}
      {tab === "approvals" && approver && <Approvals token={token} me={me.username} />}
      {tab === "runs" && <History token={token} />}
    </div>
  );
}
