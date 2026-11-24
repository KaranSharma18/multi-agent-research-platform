"""Argument shapes for write actions, shared by the orchestrator, executor and actions server.

An approval is bound to the SHA-256 of the action's arguments. The orchestrator
hashes them when a person approves; the actions server hashes them again when
the call arrives. Both sides must therefore build exactly the same dictionary,
defaults included, which is what `normalize_args` guarantees.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from mara_core.hashing import args_hash

ActionTool = Literal["create_github_issue", "post_slack_message", "send_email"]


class CreateGithubIssue(BaseModel):
    repo: str
    title: str = Field(max_length=120)
    body: str = Field(default="", max_length=4000)
    labels: list[str] = Field(default_factory=list)
    assignee: str | None = Field(default=None, pattern=r"^@?[A-Za-z0-9-]{1,39}$")


class PostSlackMessage(BaseModel):
    channel: str
    text: str = Field(max_length=3000)


class SendEmail(BaseModel):
    to: str
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)


ACTION_MODELS: dict[str, type[BaseModel]] = {
    "create_github_issue": CreateGithubIssue,
    "post_slack_message": PostSlackMessage,
    "send_email": SendEmail,
}

RISK = {"post_slack_message": "low", "create_github_issue": "medium", "send_email": "high"}


class InvalidAction(ValueError):
    pass


def normalize_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    model = ACTION_MODELS.get(tool)
    if model is None:
        raise InvalidAction(f"unknown action tool {tool!r}")
    try:
        return model.model_validate(args).model_dump(mode="json")
    except ValidationError as exc:
        raise InvalidAction(exc.errors()[0].get("msg", str(exc))) from exc


def action_hash(tool: str, args: dict[str, Any]) -> str:
    return args_hash(normalize_args(tool, args))


def render_preview(tool: str, args: dict[str, Any]) -> str:
    """What a person sees before approving: exactly what will be sent."""
    a = normalize_args(tool, args)
    if tool == "create_github_issue":
        assignee = f"\nAssignee: {a['assignee']}" if a.get("assignee") else ""
        labels = f"\nLabels: {', '.join(a['labels'])}" if a.get("labels") else ""
        return f"Open issue in {a['repo']}{assignee}{labels}\n\n# {a['title']}\n\n{a['body']}"
    if tool == "post_slack_message":
        return f"Post to {a['channel']}:\n\n{a['text']}"
    return f"Email to {a['to']}\nSubject: {a['subject']}\n\n{a['body']}"
