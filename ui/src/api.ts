// Talking to the orchestrator and the dev token service.
//
// The UI holds only the signed-in user's short-lived access token (in memory
// and sessionStorage). It never sees task tokens, approval tokens or keys.

export const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
export const TOKEN_URL = (import.meta.env.VITE_TOKEN_URL as string | undefined) ?? "http://localhost:8090/token";

export type Me = { subject: string; username: string; name?: string; groups: string[]; roles: string[] };

export type RunEvent = { id: number; type: string; data: any };

export type Action = {
  id: string;
  run_id?: string;
  tool: string;
  args?: Record<string, unknown>;
  preview: string;
  risk: string;
  status: string;
  policy_reason?: string | null;
  approver?: string | null;
  result?: any;
  error?: string | null;
  question?: string;
  username?: string;
  created_at?: string;
};

export type Citation = { id: string; title: string; uri: string; source: string; snippet: string; flagged: boolean };

export type Final = {
  status: string;
  answer: string;
  citations: Citation[];
  assumptions: string[];
  notes: string[];
  verification: { passed: boolean; issues: string[] } | null;
  actions: Action[];
  tasks: { id: string; kind: string; goal: string; status: string; error?: string | null }[];
  cost: { total_usd: number; llm_calls: number; seconds: number };
  prompt_versions: Record<string, string>;
};

export type Run = {
  id: string;
  status: string;
  question: string;
  username: string;
  created_at: string;
  cost_usd: number;
  final: Final | null;
  actions: Action[];
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function login(username: string, password: string): Promise<string> {
  const body = new URLSearchParams({ grant_type: "password", client_id: "mara-ui", username, password });
  const resp = await fetch(TOKEN_URL, { method: "POST", body });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(resp.status, data.error_description ?? "login failed");
  return data.access_token as string;
}

async function call<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(API_URL + path, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(resp.status, typeof data.detail === "string" ? data.detail : resp.statusText);
  return data as T;
}

export const api = {
  me: (t: string) => call<Me>(t, "/v1/me"),
  ask: (t: string, question: string, allowActions: boolean) =>
    call<{ run_id: string }>(t, "/v1/runs", {
      method: "POST",
      body: JSON.stringify({ question, allow_actions: allowActions }),
    }),
  run: (t: string, id: string) => call<Run>(t, `/v1/runs/${id}`),
  runs: (t: string) => call<{ runs: Run[] }>(t, "/v1/runs?limit=50"),
  cancel: (t: string, id: string) => call(t, `/v1/runs/${id}/cancel`, { method: "POST" }),
  approvals: (t: string) => call<{ actions: Action[] }>(t, "/v1/approvals?status=pending_approval"),
  decide: (t: string, id: string, approve: boolean, note?: string) =>
    call<{ status: string }>(t, `/v1/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ decision: approve ? "approve" : "reject", note }),
    }),
};

// Server-sent events over fetch, so the Authorization header can be sent
// (the browser's EventSource cannot set headers). Reconnects with Last-Event-ID.
export async function followRun(
  token: string,
  runId: string,
  onEvent: (e: RunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let lastId = 0;
  while (!signal.aborted) {
    try {
      const resp = await fetch(`${API_URL}/v1/runs/${runId}/events?after=${lastId}`, {
        headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
        signal,
      });
      if (!resp.ok || !resp.body) throw new ApiError(resp.status, "event stream failed");
      const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return; // the server closes the stream after run_finished
        buffer += value.replace(/\r\n?/g, "\n"); // SSE allows CRLF, LF or CR line endings
        let cut: number;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          let id = 0;
          let type = "message";
          const data: string[] = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("id:")) id = Number(line.slice(3).trim());
            else if (line.startsWith("event:")) type = line.slice(6).trim();
            else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
          }
          if (!data.length) continue; // keep-alive ping
          lastId = id || lastId;
          onEvent({ id, type, data: JSON.parse(data.join("\n")) });
        }
      }
    } catch (err) {
      if (signal.aborted) return;
      await new Promise((r) => setTimeout(r, 1500)); // network blip: resume from lastId
    }
  }
}
