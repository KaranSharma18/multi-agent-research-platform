import { useCallback, useEffect, useState } from "react";
import { api, type Action } from "../api";

export function Approvals({ token, me }: { token: string; me: string }) {
  const [items, setItems] = useState<Action[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems((await api.approvals(token)).actions);
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }, [token]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [load]);

  async function decide(a: Action, approve: boolean) {
    setBusy(a.id);
    try {
      await api.decide(token, a.id, approve, approve ? undefined : "rejected in the inbox");
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <h2>Approvals</h2>
      <p className="muted">
        Each action shows exactly what will be sent. Approving signs a one-time token for these exact arguments;
        nothing else can be done with it.
      </p>
      {error && <p className="error">{error}</p>}
      {!items.length && <p className="muted">Nothing waiting for approval.</p>}
      {items.map((a) => (
        <section key={a.id} className="card action">
          <header>
            <strong>{a.tool.replace(/_/g, " ")}</strong>
            <span className={`pill risk-${a.risk}`}>risk: {a.risk}</span>
            <span className="muted">asked by {a.username}{a.username === me ? " (you)" : ""} · {a.run_id}</span>
          </header>
          {a.question && <p className="muted">“{a.question}”</p>}
          <pre className="preview">{a.preview}</pre>
          <div className="buttons">
            <button disabled={busy === a.id} onClick={() => decide(a, true)}>Approve</button>
            <button disabled={busy === a.id} className="secondary" onClick={() => decide(a, false)}>Reject</button>
          </div>
        </section>
      ))}
    </div>
  );
}
