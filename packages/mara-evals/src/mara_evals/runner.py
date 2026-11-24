"""Run eval suites end to end against the running platform.

Each case goes through the real API as a real dev user: the planner, workers,
MCP servers, research agent, approvals and executor all run. A simulated
approver answers approval requests: when a case says `approve: true` it
approves everything pending without reading it. That is deliberately naive: it
tests whether the automatic layers (allowlists, policy, PII checks) hold on
their own, with the human taken out of the loop.

Results go to `data/generated/eval_reports/` as JSON and Markdown, and to a
Braintrust experiment when BRAINTRUST_API_KEY is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from mara_core.api_client import ApiError, MaraClient
from mara_core.paths import data_dir
from mara_core.settings import get_settings
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from mara_evals.datasets import as_of, load
from mara_evals.scorers import Score, score_case
from mara_evals.trajectory import trajectory_match

console = Console(highlight=False)
TERMINAL = {"completed", "failed", "canceled", "needs_clarification", "refused"}
CASE_TIMEOUT_SECONDS = 360


class Tokens:
    """Dev logins, refreshed before they expire (user tokens last 15 minutes)."""

    def __init__(self, client: MaraClient):
        self.client = client
        self._cache: dict[str, tuple[str, float]] = {}

    async def get(self, user: str) -> str:
        token, fetched = self._cache.get(user, ("", 0.0))
        if not token or time.monotonic() - fetched > 600:
            token = await self.client.token(user)
            self._cache[user] = (token, time.monotonic())
        return token


async def run_case(
    client: MaraClient, tokens: Tokens, case: dict, today: str, sem: asyncio.Semaphore, judge: Any = None
) -> dict[str, Any]:
    settings = get_settings()
    async with sem:
        started = time.monotonic()
        user_token = await tokens.get(case["user"])
        approver = "admin" if case["user"] == "bob" else "bob"
        opts = case.get("options") or {}
        try:
            run_id = await client.ask(
                user_token,
                case["question"],
                as_of=today,
                allow_actions=opts.get("allow_actions", True),
                faults=opts.get("faults"),
                research_stub=opts.get("research_stub"),
            )
        except ApiError as exc:
            return {"case": case, "error": str(exc), "score": None}
        decided: set[str] = set()
        run: dict = {}
        while True:
            run = await client.run(user_token, run_id)
            if run["status"] == "awaiting_approval":
                approver_token = await tokens.get(approver)
                for action in run.get("actions", []):
                    if action["status"] == "pending_approval" and action["id"] not in decided:
                        decided.add(action["id"])
                        try:
                            await client.decide(
                                approver_token,
                                action["id"],
                                approve=case.get("approve") is True,
                                note="simulated approver",
                            )
                        except ApiError as exc:
                            console.print(f"[dim]{case['id']}: approver could not decide {action['id']}: {exc}[/]")
            elif run["status"] in TERMINAL and not any(
                a["status"] in ("approved", "executing") for a in run.get("actions", [])
            ):
                break
            if time.monotonic() - started > CASE_TIMEOUT_SECONDS:
                with contextlib.suppress(ApiError):
                    await client.cancel(user_token, run_id)
                run = await client.run(user_token, run_id)
                break
            await asyncio.sleep(0.5)
        tool_calls = await client.tool_calls(user_token, run_id)
        score = score_case(case, run, tool_calls, settings)
        extra: dict[str, Any] = {}
        if case["expect"].get("tools_all") or case["expect"].get("tools_any"):
            extra["agentevals_trajectory"] = trajectory_match(case, tool_calls)
        if judge is not None and case["expect"].get("facts"):
            extra["factuality"] = await judge(case, run)
        return {
            "case": case,
            "run_id": run_id,
            "score": score,
            "extra": extra,
            "answer": ((run.get("final") or {}).get("answer") or "")[:2000],
            "actions": [{k: a.get(k) for k in ("tool", "status", "policy_reason")} for a in run.get("actions", [])],
        }


def summarize(scores: list[Score]) -> dict[str, Any]:
    def rate(values: list[bool]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    costs = sorted(s.cost_usd for s in scores)
    latency = sorted(s.seconds for s in scores)

    def pct(values: list[float], q: float) -> float:
        return round(values[min(int(q * len(values)), len(values) - 1)], 4) if values else 0.0

    by_category: dict[str, dict] = {}
    for s in scores:
        c = by_category.setdefault(s.category, {"cases": 0, "passed": 0, "completed": 0, "safe": 0})
        c["cases"] += 1
        c["passed"] += s.passed
        c["completed"] += s.completion
        c["safe"] += s.safety
    multi = [s for s in scores if s.multi_step]
    return {
        "cases": len(scores),
        "pass_rate": rate([s.passed for s in scores]),
        "task_completion_rate": rate([s.completion for s in scores]),
        "multi_step_completion_rate": rate([s.completion for s in multi]),
        "multi_step_cases": len(multi),
        "safety_rate": rate([s.safety for s in scores]),
        "attack_success_rate": round(1 - (rate([s.safety for s in scores]) or 0), 4),
        "trajectory_rate": rate([s.trajectory for s in scores if s.trajectory is not None]),
        "cost_per_run_usd": {
            "mean": round(statistics.fmean(costs), 5) if costs else 0.0,
            "median": pct(costs, 0.5),
            "p95": pct(costs, 0.95),
        },
        "latency_seconds": {"p50": pct(latency, 0.5), "p95": pct(latency, 0.95)},
        "llm_calls_mean": round(statistics.fmean(s.llm_calls for s in scores), 2) if scores else 0,
        "by_category": by_category,
    }


def _markdown(suite: str, summary: dict, results: list[dict], meta: dict) -> str:
    lines = [
        f"# Eval report: {suite}",
        "",
        f"- run at: {meta['started']} (as of {meta['as_of']})",
        f"- models: {meta['models']}",
        f"- prompts: {meta['prompts']}",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key in (
        "cases",
        "pass_rate",
        "task_completion_rate",
        "multi_step_completion_rate",
        "safety_rate",
        "attack_success_rate",
        "trajectory_rate",
        "llm_calls_mean",
    ):
        lines.append(f"| {key} | {summary[key]} |")
    lines.append(
        f"| cost per run (mean / p95) | ${summary['cost_per_run_usd']['mean']} / "
        f"${summary['cost_per_run_usd']['p95']} |"
    )
    lines.append(
        f"| latency p50 / p95 (s) | {summary['latency_seconds']['p50']} / {summary['latency_seconds']['p95']} |"
    )
    lines += ["", "| category | cases | passed | completed | safe |", "|---|---|---|---|---|"]
    for cat, c in sorted(summary["by_category"].items()):
        lines.append(f"| {cat} | {c['cases']} | {c['passed']} | {c['completed']} | {c['safe']} |")
    failed = [r for r in results if r.get("score") and not r["score"].passed]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append(
                f"- **{r['case']['id']}** ({r['case']['category']}) `{r.get('run_id')}`: "
                + "; ".join(r["score"].reasons[:4])
            )
    return "\n".join(lines) + "\n"


def _braintrust(suite: str, results: list[dict], meta: dict) -> str | None:
    if not os.environ.get("BRAINTRUST_API_KEY"):
        return None
    import braintrust

    settings = get_settings()
    experiment = braintrust.init(
        project=settings.braintrust_project, experiment=f"{suite}-{meta['started']}", metadata=meta
    )
    for r in results:
        s: Score | None = r.get("score")
        if s is None:
            continue
        scores = {"passed": float(s.passed), "completion": float(s.completion), "safety": float(s.safety)}
        if s.trajectory is not None:
            scores["trajectory"] = float(s.trajectory)
        for key, value in (r.get("extra") or {}).items():
            if isinstance(value, (int, float)):
                scores[key] = float(value)
        experiment.log(
            input={"question": r["case"]["question"], "user": r["case"]["user"], "options": r["case"].get("options")},
            output={"answer": r.get("answer"), "actions": r.get("actions"), "status": s.status},
            expected=r["case"]["expect"],
            scores=scores,
            metadata={"case_id": s.case_id, "category": s.category, "run_id": r.get("run_id"), "reasons": s.reasons},
            metrics={"cost_usd": s.cost_usd, "llm_calls": s.llm_calls, "latency_s": s.seconds},
        )
    summary = experiment.summarize()
    return getattr(summary, "experiment_url", None)


async def run_suites(
    suites: list[str],
    *,
    limit: int | None = None,
    concurrency: int = 4,
    category: str | None = None,
    judge: bool = False,
) -> dict[str, dict]:
    settings = get_settings()
    client = MaraClient(settings)
    tokens = Tokens(client)
    judge_fn = None
    if judge:
        from mara_evals.judge import make_judge

        judge_fn = make_judge(settings)
    reports: dict[str, dict] = {}
    try:
        today = as_of()
        from mara_orchestrator.prompts import versions

        for suite in suites:
            cases = load(suite)
            if category:
                cases = [c for c in cases if c["category"] == category]
            cases = cases[:limit] if limit else cases
            started = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            meta = {
                "suite": suite,
                "started": started,
                "as_of": today,
                "prompts": versions(),
                "models": f"{settings.llm_provider}:{settings.model_mid}/{settings.model_small}",
            }
            console.print(f"[bold]{suite}[/]: {len(cases)} cases, concurrency {concurrency}")
            sem = asyncio.Semaphore(concurrency)
            done = 0

            async def one(c: dict, sem: asyncio.Semaphore = sem, total: int = len(cases)) -> dict:
                nonlocal done
                result = await run_case(client, tokens, c, today, sem, judge_fn)
                done += 1
                s = result.get("score")
                mark = "[green]pass[/]" if s and s.passed else "[red]fail[/]"
                console.print(
                    f"  {done:>3}/{total} {mark} {c['id']} {c['category']}"
                    + (f" [dim]{escape('; '.join(s.reasons[:2]))}[/]" if s and not s.passed else "")
                )
                return result

            results = await asyncio.gather(*(one(c) for c in cases))
            scores = [r["score"] for r in results if r.get("score")]
            summary = summarize(scores)
            out = data_dir() / "eval_reports"
            out.mkdir(parents=True, exist_ok=True)
            stem = out / f"{suite}-{started}"
            stem.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "meta": meta,
                        "summary": summary,
                        "results": [
                            {
                                **{k: v for k, v in r.items() if k != "score"},
                                "score": asdict(r["score"]) if r.get("score") else None,
                            }
                            for r in results
                        ],
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            stem.with_suffix(".md").write_text(_markdown(suite, summary, results, meta), encoding="utf-8")
            url = _braintrust(suite, results, meta)
            table = Table(title=f"{suite} ({summary['cases']} cases)", box=None)
            table.add_column("metric")
            table.add_column("value", justify="right")
            for key in (
                "pass_rate",
                "task_completion_rate",
                "multi_step_completion_rate",
                "safety_rate",
                "attack_success_rate",
                "trajectory_rate",
            ):
                table.add_row(key, "-" if summary[key] is None else f"{summary[key]:.1%}")
            table.add_row("cost per run (mean)", f"${summary['cost_per_run_usd']['mean']:.4f}")
            table.add_row(
                "latency p50 / p95", f"{summary['latency_seconds']['p50']}s / {summary['latency_seconds']['p95']}s"
            )
            console.print(table)
            console.print(
                f"[dim]report: {stem.with_suffix('.md')}[/]" + (f"\n[dim]braintrust: {url}[/]" if url else "")
            )
            reports[suite] = summary
    finally:
        await client.aclose()
    return reports
