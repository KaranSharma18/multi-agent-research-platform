import type { RunEvent } from "../api";

function describe(e: RunEvent): { text: string; tone?: string } | null {
  const d = e.data;
  switch (e.type) {
    case "guard":
      return d.flagged ? { text: "Question flagged as possible injection: write actions off", tone: "warn" } : null;
    case "plan":
      return {
        text:
          (d.replan ? "Replanned: " : "Plan: ") +
          d.tasks.map((t: any) => `${t.id} ${t.kind}${t.depends_on?.length ? ` (after ${t.depends_on.join(", ")})` : ""}`).join(" · "),
      };
    case "task_started":
      return { text: `${d.task_id} started (${d.kind}): ${d.goal.length > 140 ? d.goal.slice(0, 140) + "…" : d.goal}` };
    case "tool_call":
      return { text: `${d.task_id} → ${d.server}.${d.tool} ${d.ok ? "ok" : "failed"} · ${d.latency_ms} ms`, tone: d.ok ? "dim" : "warn" };
    case "a2a_state":
      return { text: `${d.task_id} research agent: ${d.state}`, tone: "dim" };
    case "task_finished":
      return { text: `${d.task_id} ${d.status} · ${d.evidence} evidence · ${d.seconds}s`, tone: d.status === "done" ? "ok" : "warn" };
    case "tasks_skipped":
      return { text: `Skipped: ${d.task_ids.join(", ")}`, tone: "warn" };
    case "answer_draft":
      return { text: d.revision ? "Revised the answer after the check" : "Drafted the answer" };
    case "verification":
      return d.passed ? { text: "Verification passed", tone: "ok" } : { text: `Verification flagged: ${d.issues.slice(0, 2).join("; ")}`, tone: "warn" };
    case "actions_proposed":
      return { text: `Proposed ${d.actions.length} action(s)` };
    case "approval_required":
      return { text: "Waiting for an approver", tone: "warn" };
    case "action_decided":
      return { text: `${d.action_id} ${d.status} by ${d.approver}` };
    case "action_executed":
      return { text: `${d.action_id} executed`, tone: "ok" };
    case "action_failed":
      return { text: `${d.action_id} failed: ${d.error}`, tone: "warn" };
    case "error":
      return { text: d.message, tone: "warn" };
    default:
      return null;
  }
}

export function Timeline({ events }: { events: RunEvent[] }) {
  const rows = events.map((e) => ({ e, d: describe(e) })).filter((r) => r.d);
  if (!rows.length) return null;
  return (
    <section className="card timeline">
      <h3>Progress</h3>
      <ol>
        {rows.map(({ e, d }) => (
          <li key={e.id} className={d!.tone}>{d!.text}</li>
        ))}
      </ol>
    </section>
  );
}
