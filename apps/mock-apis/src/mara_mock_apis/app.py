"""Incident API, status page, GitHub-like issues and PRs, Slack webhooks and FX rates.

Data comes from `data/generated/fixtures/*.json`, written by `mara seed`.
Writes (new issues, Slack posts) are kept in memory and mirrored to
`data/generated/sandbox/` so you can inspect what an approved action did.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from mara_core.logging import get_logger
from mara_core.paths import data_dir
from pydantic import BaseModel

log = get_logger(__name__)


def _load(name: str, default: Any) -> Any:
    path = data_dir() / "fixtures" / name
    if not path.exists():
        log.warning("fixture_missing", file=str(path), hint="run `uv run mara seed` first")
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class NewIssue(BaseModel):
    title: str
    body: str = ""
    labels: list[str] = []
    assignees: list[str] = []


class SlackMessage(BaseModel):
    channel: str | None = None
    text: str


def create_app() -> FastAPI:
    app = FastAPI(title="MARA mock APIs", version="0.1.0")
    lock = threading.Lock()
    sandbox_dir: Path = data_dir() / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    state: dict[str, Any] = {
        "incidents": _load("incidents.json", []),
        "timelines": _load("incident_timelines.json", {}),
        "status": _load("status.json", {}),
        "github": _load("github.json", {"repos": {}}),
        "fx": _load("fx.json", {"base": "USD", "rates": {}, "daily_drift": {}}),
        "slack": [],
    }

    def persist() -> None:
        created = {
            repo: [i for i in data["issues"] if i.get("sandbox")] for repo, data in state["github"]["repos"].items()
        }
        (sandbox_dir / "github_created.json").write_text(json.dumps(created, indent=2), encoding="utf-8")
        (sandbox_dir / "slack_messages.json").write_text(json.dumps(state["slack"], indent=2), encoding="utf-8")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "incidents": str(len(state["incidents"]))}

    # ------------------------------------------------------------- incidents
    @app.get("/incidents")
    def list_incidents(
        since: str | None = None,
        until: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        limit: int = Query(50, le=200),
    ) -> list[dict]:
        out = []
        for inc in state["incidents"]:
            if service and inc["service"] != service:
                continue
            if severity and inc["severity"] != severity:
                continue
            day = inc["started_at"][:10]  # compare dates, so "until" includes that whole day
            if since and day < since[:10]:
                continue
            if until and day > until[:10]:
                continue
            out.append({k: v for k, v in inc.items() if k not in ("customers_affected",)})
        return out[:limit]

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str) -> dict:
        for inc in state["incidents"]:
            if inc["id"].lower() == incident_id.lower():
                return inc
        raise HTTPException(404, f"incident {incident_id} not found")

    @app.get("/incidents/{incident_id}/timeline")
    def get_timeline(incident_id: str) -> list[dict]:
        timeline = state["timelines"].get(incident_id.upper())
        if timeline is None:
            raise HTTPException(404, f"no timeline for {incident_id}")
        return timeline

    @app.get("/status/{service}")
    def service_status(service: str) -> dict:
        history = state["status"].get(service)
        if history is None:
            raise HTTPException(404, f"unknown service {service}")
        return {
            "service": service,
            "current": history[-1]["status"] if history else "unknown",
            "history": history[-14:],
        }

    # ---------------------------------------------------------------- github
    def _repo(owner: str, repo: str) -> dict:
        full = f"{owner}/{repo}"
        repos = state["github"]["repos"]
        if full not in repos:
            raise HTTPException(404, f"repository {full} not found")
        return repos[full]

    @app.get("/github/search/issues")
    def search_issues(q: str, state_filter: str = Query("all", alias="state"), limit: int = 20) -> dict:
        terms = [t.lower() for t in q.split() if ":" not in t]
        repo_filter = next((t.split(":", 1)[1] for t in q.split() if t.startswith("repo:")), None)
        items = []
        for full, data in state["github"]["repos"].items():
            if repo_filter and full != repo_filter:
                continue
            for issue in data["issues"]:
                if state_filter != "all" and issue["state"] != state_filter:
                    continue
                text = f"{issue['title']} {issue.get('body', '')}".lower()
                if all(t in text for t in terms):
                    items.append({**issue, "repo": full})
        items.sort(key=lambda i: i["created_at"], reverse=True)
        return {"total_count": len(items), "items": items[:limit]}

    @app.get("/github/repos/{owner}/{repo}/issues/{number}")
    def get_issue(owner: str, repo: str, number: int) -> dict:
        for issue in _repo(owner, repo)["issues"]:
            if issue["number"] == number:
                return {**issue, "repo": f"{owner}/{repo}"}
        raise HTTPException(404, "issue not found")

    @app.post("/github/repos/{owner}/{repo}/issues", status_code=201)
    def create_issue(owner: str, repo: str, payload: NewIssue) -> dict:
        with lock:
            data = _repo(owner, repo)
            number = max([i["number"] for i in data["issues"]] + [0]) + 1
            issue = {
                "number": number,
                "title": payload.title,
                "body": payload.body,
                "state": "open",
                "labels": payload.labels,
                "assignee": payload.assignees[0] if payload.assignees else None,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "sandbox": True,
            }
            data["issues"].append(issue)
            persist()
        log.info("sandbox_issue_created", repo=f"{owner}/{repo}", number=number)
        return {**issue, "html_url": f"https://github.com/{owner}/{repo}/issues/{number}"}

    @app.get("/github/repos/{owner}/{repo}/pulls")
    def list_pulls(owner: str, repo: str, state_filter: str = Query("open", alias="state")) -> list[dict]:
        pulls = _repo(owner, repo).get("pulls", [])
        return [p for p in pulls if state_filter == "all" or p["state"] == state_filter]

    @app.get("/github/repos/{owner}/{repo}/pulls/{number}")
    def get_pull(owner: str, repo: str, number: int) -> dict:
        for pull in _repo(owner, repo).get("pulls", []):
            if pull["number"] == number:
                return pull
        raise HTTPException(404, "pull request not found")

    # ----------------------------------------------------------------- slack
    @app.post("/slack/webhook/{channel}")
    def slack_post(channel: str, payload: SlackMessage) -> dict:
        with lock:
            message = {
                "channel": f"#{channel.lstrip('#')}",
                "text": payload.text,
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            state["slack"].append(message)
            persist()
        return {"ok": True, "ts": message["ts"]}

    @app.get("/slack/messages")
    def slack_messages(channel: str | None = None) -> list[dict]:
        return [m for m in state["slack"] if channel is None or m["channel"] == channel]

    # -------------------------------------------------------------------- fx
    @app.get("/fx")
    def fx(base: str = "USD", quote: str = "EUR", date: str | None = None) -> dict:
        rates = state["fx"].get("rates", {})
        base, quote = base.upper(), quote.upper()
        if base != "USD" and base not in rates:
            raise HTTPException(404, f"unknown currency {base}")
        if quote != "USD" and quote not in rates:
            raise HTTPException(404, f"unknown currency {quote}")
        day = date or datetime.now(UTC).date().isoformat()
        drift = 1 + (sum(map(ord, day)) % 7 - 3) / 1000  # deterministic per day
        usd_to_quote = 1.0 if quote == "USD" else rates[quote] * drift
        usd_to_base = 1.0 if base == "USD" else rates[base] * drift
        return {"base": base, "quote": quote, "date": day, "rate": round(usd_to_quote / usd_to_base, 6)}

    return app
