"""Write actions: propose, check, wait for a person, then wait for the executor.

The chain of custody for every write:
  1. propose: the small model drafts actions from the verified answer;
  2. policy (code): arguments validated, repo/channel/domain allowlisted,
     no personal data, links outside url_domains stripped, assignee must come
     from the user's request or typed warehouse rows, rate limits applied;
  3. approve: the graph pauses (LangGraph interrupt) until a person decides in
     the approvals inbox. Approving mints a signed, one-time token bound to the
     SHA-256 of the exact arguments the person saw (api/approvals.py);
  4. execute: a separate executor service, holding the only write credentials,
     calls the actions MCP server with that token. This node just waits for the
     outcome so the final answer can report it.

The orchestrator itself never holds write scopes. A prompt injection that gets
as far as a proposal still has to get past code checks and a person, and even
then it can only do exactly what the person approved.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from mara_core.actions import RISK, InvalidAction, action_hash, normalize_args, render_preview
from mara_core.config import Allowlists, allowlists
from mara_core.logging import get_logger
from mara_core.pii import contains_pii
from mara_core.spotlight import UNTRUSTED_RULE

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.state import ActionProposals, RunState
from mara_orchestrator.llm.cost import BudgetExceeded
from mara_orchestrator.llm.models import structured
from mara_orchestrator.prompts import render, split

log = get_logger(__name__)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
EXECUTE_WAIT_SECONDS = 60
PUBLIC_FIELDS = ("id", "tool", "args", "preview", "risk", "reason", "status", "policy_reason", "args_sha256")


def typed_facts(evidence: list[dict], max_rows: int = 25) -> str:
    """Rows from the warehouse, as typed data. The only data source allowed to supply assignees."""
    blocks = []
    for item in evidence:
        data = item.get("data") or {}
        if item.get("source") != "sql" or not data.get("columns"):
            continue
        cols = data["columns"]
        rows = data.get("rows") or []
        lines = [" | ".join(cols)] + [" | ".join(str(r.get(c, "")) for c in cols) for r in rows[:max_rows]]
        blocks.append(f"[{item['id']}]\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _strip_links(text: str, allowed: list[str]) -> tuple[str, int]:
    removed = 0

    def repl(match: re.Match) -> str:
        nonlocal removed
        host = (urlparse(match.group(0)).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in allowed):
            return match.group(0)
        removed += 1
        return "[link removed]"

    return URL_RE.sub(repl, text), removed


def check_action(
    tool: str, args: dict, *, allow: Allowlists, question: str, typed: str
) -> tuple[dict | None, str | None]:
    """Return (normalized args, None) if the action may be shown to a person, else (None, reason)."""
    try:
        clean = normalize_args(tool, args)
    except InvalidAction as exc:
        return None, f"invalid arguments: {exc}"
    text_fields = {
        "create_github_issue": ("title", "body"),
        "post_slack_message": ("text",),
        "send_email": ("subject", "body"),
    }[tool]
    for field in text_fields:
        clean[field], _ = _strip_links(clean[field], allow.url_domains)
        if contains_pii(clean[field]):
            return None, f"the {field} contains personal data"
    if tool == "create_github_issue":
        if clean["repo"] not in allow.github_repos:
            return None, f"repo {clean['repo']} is not on the allowlist"
        assignee = (clean.get("assignee") or "").lstrip("@")
        if assignee and assignee.lower() not in (question + "\n" + typed).lower():
            return None, f"assignee {assignee} does not come from your request or from warehouse data"
    elif tool == "post_slack_message":
        if clean["channel"] not in allow.slack_channels:
            return None, f"channel {clean['channel']} is not on the allowlist"
    elif tool == "send_email":
        if not allow.email_enabled:
            return None, "email is turned off in config/allowlists.yaml"
        domain = clean["to"].rsplit("@", 1)[-1].lower()
        if domain not in allow.email_domains:
            return None, f"email domain {domain} is not on the allowlist"
    return clean, None


async def propose(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    allow = allowlists()
    typed = typed_facts(state.get("evidence") or [])
    rendered, _ = render(
        "proposer",
        repos=", ".join(allow.github_repos),
        channels=", ".join(allow.slack_channels),
        email_note=(
            f"domains allowed: {', '.join(allow.email_domains)}"
            if allow.email_enabled
            else "turned off; do not propose email"
        ),
        max_actions=allow.limits.max_actions_per_run,
        untrusted_rule=UNTRUSTED_RULE,
        question=state["question"],
        answer=state.get("answer") or "",
        typed_facts=typed or "(none)",
    )
    system, human = split(rendered, "User request:")
    try:
        ctx.budget.check()
        proposals: ActionProposals = await structured("small", ActionProposals).ainvoke(
            [SystemMessage(system), HumanMessage(human)]
        )
    except BudgetExceeded:
        return {"stop_reason": "budget"}
    except Exception:
        log.exception("proposer_failed")
        await ctx.emit("error", {"stage": "propose", "message": "Could not draft the requested actions."})
        return {}

    user = state.get("user") or {}
    requester = user.get("username") or user.get("subject") or "unknown"
    used_today = await ctx.services.repo.actions_today(requester)
    accepted = 0
    actions: dict[str, dict] = {}
    for n, draft in enumerate(proposals.actions[: allow.limits.max_actions_per_run], start=1):
        action_id = f"{state['run_id']}-a{n}"  # deterministic: re-running this node cannot duplicate actions
        draft_args = draft.to_args()
        clean, reason = check_action(draft.tool, draft_args, allow=allow, question=state["question"], typed=typed)
        if reason is None and used_today + accepted >= allow.limits.max_actions_per_user_per_day:
            reason = "daily action limit reached for this user"
        args = clean or draft_args
        record = {
            "id": action_id,
            "run_id": state["run_id"],
            "tool": draft.tool,
            "args": args,
            "args_sha256": action_hash(draft.tool, args) if clean else "",
            "preview": render_preview(draft.tool, args) if clean else f"{draft.tool}: {str(draft_args)[:300]}",
            "risk": RISK[draft.tool],
            "reason": draft.reason[:300],
            "requested_by": requester,
            "status": "rejected_by_policy" if reason else "pending_approval",
            "policy_reason": reason,
            "idempotency_key": action_id,
        }
        if reason is None:
            accepted += 1
        await ctx.services.repo.insert_action(record)
        await ctx.services.repo.audit(
            "action_proposed",
            actor="orchestrator",
            run_id=state["run_id"],
            data={
                "action_id": action_id,
                "tool": draft.tool,
                "status": record["status"],
                "policy_reason": reason,
                "args_sha256": record["args_sha256"],
            },
        )
        actions[action_id] = {k: record[k] for k in PUBLIC_FIELDS}
    await ctx.emit("actions_proposed", {"actions": list(actions.values())})
    return {"actions": actions, "cost_checkpoint": ctx.budget.snapshot()}


def route_after_propose(state: RunState) -> str:
    pending = [a for a in (state.get("actions") or {}).values() if a["status"] == "pending_approval"]
    return "approve" if pending else "finalize"


async def approve(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """Pause until every pending action is decided. Decisions are read from the database.

    LangGraph re-runs a node from the top when it resumes, so nothing before
    `interrupt()` may have side effects. The runner records the pause.
    """
    ctx = runtime.context
    pending = [a["id"] for a in (state.get("actions") or {}).values() if a["status"] == "pending_approval"]
    interrupt({"type": "approval", "run_id": state["run_id"], "action_ids": pending})
    rows = {r["id"]: r for r in await ctx.services.repo.actions_for_run(state["run_id"])}
    updates = {}
    for action_id in pending:
        row = rows.get(action_id) or {}
        status = row.get("status", "expired")
        if status == "pending_approval":
            status = "expired"
        updates[action_id] = {
            "status": status,
            "approver": row.get("approver"),
            "decision_note": row.get("decision_note"),
        }
    return {"actions": updates}


async def execute(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """Wait (bounded) for the executor to run the approved actions."""
    ctx = runtime.context
    approved = [a["id"] for a in (state.get("actions") or {}).values() if a["status"] in ("approved", "executing")]
    if not approved:
        return {}
    await ctx.services.repo.update_run(state["run_id"], status="executing")
    await ctx.emit("executing", {"action_ids": approved})
    deadline = time.monotonic() + EXECUTE_WAIT_SECONDS
    rows: dict[str, dict] = {}
    while True:
        rows = {r["id"]: r for r in await ctx.services.repo.actions_for_run(state["run_id"])}
        if (
            all(rows.get(i, {}).get("status") in ("executed", "failed") for i in approved)
            or time.monotonic() > deadline
        ):
            break
        await asyncio.sleep(1.0)
    return {
        "actions": {
            i: {
                "status": rows.get(i, {}).get("status", "approved"),
                "result": rows.get(i, {}).get("result"),
                "error": rows.get(i, {}).get("error"),
            }
            for i in approved
        }
    }
