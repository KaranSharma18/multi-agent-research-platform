"""Where writes go: the sandbox (mock APIs, default) or the real services."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
from mara_core.settings import Settings
from mcp.server.fastmcp.exceptions import ToolError


class Backends:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mock = httpx.AsyncClient(base_url=settings.mock_apis_url, timeout=10.0)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        self.github = httpx.AsyncClient(base_url="https://api.github.com", timeout=10.0, headers=headers)

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str], assignee: str | None
    ) -> dict[str, Any]:
        payload = {"title": title, "body": body, "labels": labels, "assignees": [assignee] if assignee else []}
        if self.settings.actions_backend == "sandbox":
            resp = await self.mock.post(f"/github/repos/{repo}/issues", json=payload)
        else:
            resp = await self.github.post(f"/repos/{repo}/issues", json=payload)
        if resp.status_code >= 400:
            raise ToolError(f"GitHub returned HTTP {resp.status_code}")
        data = resp.json()
        return {"issue_url": data.get("html_url"), "issue_number": data.get("number")}

    async def post_slack(self, channel: str, text: str) -> dict[str, Any]:
        if self.settings.actions_backend == "sandbox":
            resp = await self.mock.post(
                f"/slack/webhook/{channel.lstrip('#')}", json={"channel": channel, "text": text}
            )
        else:
            if not self.settings.slack_webhook_url:
                raise ToolError("SLACK_WEBHOOK_URL is not configured")
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.settings.slack_webhook_url, json={"text": text})
        if resp.status_code >= 400:
            raise ToolError(f"Slack returned HTTP {resp.status_code}")
        return {"posted": True, "channel": channel}

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        msg = EmailMessage()
        msg["From"] = "mara-agent@acme-cloud.example"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(msg)
        return {"sent": True, "to": to}
