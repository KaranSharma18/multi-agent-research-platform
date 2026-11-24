"""Run state and the typed objects that move through it.

The checkpointed state holds plain JSON (dicts, lists, strings), not Pydantic
objects. Checkpoints outlive code versions, and plain JSON survives a renamed
class or a moved module; Pydantic models are used at the edges to validate
what models return and what APIs receive.

Workers run in parallel and write to `results`, `evidence` and `tasks` in the
same super-step, so those keys have reducers (append, merge by id) instead of
last-write-wins.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

TaskKind = Literal["docs", "sql", "live_api", "web_research"]
TaskStatus = Literal["pending", "running", "done", "failed", "skipped"]


def merge_by_id(left: dict[str, dict] | None, right: dict[str, dict] | None) -> dict[str, dict]:
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = {**merged.get(key, {}), **value}
    return merged


# ---------------------------------------------------------------- model I/O
# Model-facing schemas use typed fields only, never free-form dicts: some
# structured-output paths turn an untyped object into a string, and explicit
# fields are easier for a model to fill correctly anyway.
class TaskInput(BaseModel):
    name: str = Field(description="e.g. since, until, incident_id, customer, service")
    value: str = Field(description="A concrete value, e.g. 2026-11-14 or INC-2041")


class PlannedTask(BaseModel):
    id: str = Field(description="Short id: t1, t2, ...")
    kind: TaskKind = Field(
        description="docs = postmortems/runbooks/product docs; sql = customers, ARR, invoices, usage; "
        "live_api = incidents, service status, GitHub issues/PRs, FX; web_research = the public web"
    )
    goal: str = Field(description="One sentence telling the worker exactly what to find")
    inputs: list[TaskInput] = Field(
        default_factory=list, description="Concrete values the worker needs: dates, ids, names"
    )
    depends_on: list[str] = Field(default_factory=list, description="Ids of tasks whose results this task needs")
    tool_hints: list[str] = Field(
        default_factory=list, description="Tool names you expect it to need, if you know them"
    )


class TaskPlan(BaseModel):
    tasks: list[PlannedTask] = Field(description="At most eight; code keeps the first eight")
    answer_shape: str = Field(description="What the final answer must contain")
    needs_action: bool = Field(description="True only if the user explicitly asked to create, post or send something")
    assumptions: list[str] = Field(
        default_factory=list, description="Assumptions made about ambiguous parts of the question"
    )
    clarifying_question: str | None = Field(
        default=None, description="Ask one question instead of guessing when the request cannot be answered without it"
    )


class WorkerAnswer(BaseModel):
    """What a worker hands back. The structured-output tool the worker calls when it is done."""

    summary: str = Field(description="At most 120 words. Cite evidence ids like [ev_t1_2] after each fact.")
    facts: list[str] = Field(
        default_factory=list, description="Key facts as short sentences, each with its evidence id"
    )
    gaps: list[str] = Field(default_factory=list, description="What you could not find or verify")
    followup_needed: str | None = Field(default=None, description="A missing step another task should do, if any")


class Verdict(BaseModel):
    passed: bool
    issues: list[str] = Field(
        default_factory=list, description="Concrete problems: unsupported claims, wrong numbers, missing parts"
    )


class ProposedActionDraft(BaseModel):
    tool: Literal["create_github_issue", "post_slack_message", "send_email"]
    reason: str = Field(description="Which part of the user's request this fulfils")
    repo: str | None = Field(default=None, description="create_github_issue: owner/name")
    title: str | None = Field(default=None, description="create_github_issue: issue title")
    body: str | None = Field(default=None, description="create_github_issue / send_email: body text")
    labels: list[str] = Field(default_factory=list, description="create_github_issue: labels")
    assignee: str | None = Field(default=None, description="create_github_issue: GitHub handle")
    channel: str | None = Field(default=None, description="post_slack_message: #channel")
    text: str | None = Field(default=None, description="post_slack_message: message text")
    to: str | None = Field(default=None, description="send_email: recipient address")
    subject: str | None = Field(default=None, description="send_email: subject line")

    def to_args(self) -> dict[str, Any]:
        """The arguments for this tool only, in the shape the actions server expects."""
        if self.tool == "create_github_issue":
            return {
                "repo": self.repo or "",
                "title": self.title or "",
                "body": self.body or "",
                "labels": self.labels,
                "assignee": self.assignee,
            }
        if self.tool == "post_slack_message":
            return {"channel": self.channel or "", "text": self.text or ""}
        return {"to": self.to or "", "subject": self.subject or "", "body": self.body or ""}


class ActionProposals(BaseModel):
    actions: list[ProposedActionDraft] = Field(default_factory=list, description="Only what the user asked for")


# ------------------------------------------------------------ graph state
class RunState(TypedDict, total=False):
    run_id: str
    question: str
    as_of: str  # the date this run treats as "today"
    user: dict[str, Any]  # subject, username, groups, roles (never the token)
    options: dict[str, Any]  # allow_actions, replay_of, faults
    guard: dict[str, Any]
    plan: dict[str, Any] | None
    tasks: Annotated[dict[str, dict], merge_by_id]
    results: Annotated[list[dict], add]
    evidence: Annotated[list[dict], add]
    answer: str | None
    verdict: dict[str, Any] | None
    verify_issues: list[str]
    revisions: int
    replans: int
    handled_followups: list[str]  # task ids whose follow-up requests were already replanned
    actions: Annotated[dict[str, dict], merge_by_id]
    status: str
    stop_reason: str | None
    error: str | None
    cost_checkpoint: dict[str, Any] | None  # spend before an approval pause; the resumed part adds to it
    final: dict[str, Any] | None


class WorkerInput(TypedDict, total=False):
    """What `dispatch` sends to one worker (via LangGraph's Send)."""

    run_id: str
    task: dict[str, Any]
    question: str
    as_of: str
    user: dict[str, Any]
    dependencies: list[dict[str, Any]]
    options: dict[str, Any]
