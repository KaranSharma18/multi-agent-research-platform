"""The `mara` command line. Run `uv run mara --help`."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date
from typing import Annotated

import typer
from mara_core.paths import data_dir, repo_root, resolve
from mara_core.serve import run_coroutine
from mara_core.settings import get_settings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    help="MARA: Multi-Agent Research & Action platform, developer commands.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
db_app = typer.Typer(help="Database setup.", no_args_is_help=True)
tools_app = typer.Typer(help="MCP tool catalog and pins.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation suites.", no_args_is_help=True)
audit_app = typer.Typer(help="Audit log.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(tools_app, name="tools")
app.add_typer(eval_app, name="eval")
app.add_typer(audit_app, name="audit")
console = Console(highlight=False)


def arun(coro):
    return run_coroutine(coro, selector_loop=True)


# ------------------------------------------------------------------ setup
@app.command()
def keys(force: Annotated[bool, typer.Option(help="Replace existing keys")] = False) -> None:
    """Create the dev signing keys in .secrets/ (token service, approvals, agent card)."""
    from mara_core.auth.keys import generate_ed25519, generate_rsa

    s = get_settings()
    generate_rsa(s.token_service_key_file, overwrite=force)
    generate_ed25519(s.approval_private_key_file, s.approval_public_key_file, overwrite=force)
    generate_ed25519(s.agent_card_private_key_file, s.agent_card_public_key_file, overwrite=force)
    for path in (
        s.token_service_key_file,
        s.approval_private_key_file,
        s.approval_public_key_file,
        s.agent_card_private_key_file,
        s.agent_card_public_key_file,
    ):
        console.print(f"[green]ok[/] {resolve(path)}")
    console.print("[dim]Keys are git-ignored. Never commit .secrets/.[/]")


@db_app.command("init")
def db_init(
    superuser_url: Annotated[
        str, typer.Option(envvar="PG_SUPERUSER_URL", help="A superuser connection")
    ] = "postgresql://postgres:postgres@localhost:5432/postgres",
) -> None:
    """Create the roles (mara, warehouse, agent_ro) and databases. Safe to run twice."""
    import psycopg

    with psycopg.connect(superuser_url, autocommit=True) as conn:
        for role in ("mara", "warehouse", "agent_ro"):
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
            if not exists:
                conn.execute(f"create role {role} login password '{role}'")
                console.print(f"[green]created role[/] {role}")
        conn.execute("alter role agent_ro set default_transaction_read_only = on")
        conn.execute("alter role agent_ro set statement_timeout = '5s'")
        for name, owner in (("mara", "mara"), ("warehouse", "warehouse")):
            if not conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone():
                conn.execute(f"create database {name} owner {owner}")
                console.print(f"[green]created database[/] {name}")
        conn.execute("revoke all on database warehouse from public")
        conn.execute("grant connect on database warehouse to agent_ro")
        conn.execute("revoke all on database mara from public")
        conn.execute("grant connect on database mara to mara")
    console.print("[bold green]database ready[/]")


@app.command()
def seed(
    as_of: Annotated[str | None, typer.Option(help="Treat this date as today (YYYY-MM-DD)")] = None,
    seed_value: Annotated[int, typer.Option("--seed", help="Random seed")] = 42,
    no_warehouse: Annotated[bool, typer.Option(help="Skip the Postgres warehouse")] = False,
) -> None:
    """Generate the synthetic world: warehouse tables, mock API fixtures and the document corpus."""
    from mara_data import seed_all

    result = seed_all(
        get_settings(), date.fromisoformat(as_of) if as_of else None, seed_value, warehouse=not no_warehouse
    )
    console.print_json(data=result)
    console.print("[dim]Next: uv run mara ingest[/]")


@app.command()
def ingest() -> None:
    """Chunk, mask, scan and embed the corpus into Qdrant."""
    from mara_data.ingest import ingest as run_ingest

    s = get_settings()
    stats = arun(run_ingest(data_dir() / "corpus", s))
    console.print_json(data=stats)
    if s.embedding_provider == "hash" or s.llm_provider == "offline":
        console.print(
            "[yellow]Embedded with the offline hash embedder. Retrieval quality is much lower than "
            "with Gemini embeddings; set GOOGLE_API_KEY and EMBEDDING_PROVIDER=gemini for real use.[/]"
        )


@app.command()
def doctor() -> None:
    """Check that everything needed to run is in place."""
    import httpx
    import psycopg

    s = get_settings()
    table = Table("check", "status", "detail", box=None)

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        table.add_row(name, "[green]ok[/]" if ok else "[yellow]warn[/]" if ok is None else "[red]fail[/]", detail)

    row("python", sys.version_info >= (3, 12), sys.version.split()[0])
    row("uv", bool(shutil.which("uv")) or None, shutil.which("uv") or "not on PATH")
    row(
        ".env",
        (repo_root() / ".env").exists() or None,
        "copy .env.example to .env" if not (repo_root() / ".env").exists() else "found",
    )
    has_keys = resolve(s.approval_private_key_file).exists()
    row("keys", has_keys, "found" if has_keys else "run: uv run mara keys")
    llm_ok = s.llm_provider == "offline" or bool(s.google_api_key)
    row("models", llm_ok, f"{s.llm_provider}: {s.model_mid} / {s.model_small}" if llm_ok else "set GOOGLE_API_KEY")
    for name, url in (("app database", s.app_db_url), ("warehouse (agent_ro)", s.warehouse_ro_url)):
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("select 1")
            row(name, True, url.split("@")[-1])
        except Exception as exc:
            row(name, False, str(exc).splitlines()[0][:90])
    if s.qdrant_url.startswith("http"):
        try:
            httpx.get(s.qdrant_url.rstrip("/") + "/healthz", timeout=3).raise_for_status()
            row("qdrant", True, s.qdrant_url)
        except Exception as exc:
            row("qdrant", False, f"{s.qdrant_url}: {exc.__class__.__name__}")
    else:
        row("qdrant (embedded)", resolve(s.qdrant_url).exists() or None, str(resolve(s.qdrant_url)))
    seeded = (data_dir() / "seed_meta.json").exists()
    row("seed data", seeded, "found" if seeded else "run: uv run mara seed")
    locked = (repo_root() / "mcp-tools.lock.json").exists()
    row("tool lock", locked or None, "found" if locked else "run: uv run mara tools lock")
    research = "claude" if s.anthropic_api_key else ("gemini" if s.google_api_key else "stub")
    row("research engine", True, f"{s.research_engine} (auto picks {research})")
    console.print(table)


# ------------------------------------------------------------------ tools
@tools_app.command("lock")
def tools_lock() -> None:
    """Fetch every read server's tool list and pin it in mcp-tools.lock.json. Review the diff before committing."""
    from mara_orchestrator.auth.broker import TokenBroker
    from mara_orchestrator.tools.registry import ToolRegistry, lock_path, write_lock

    async def go() -> None:
        s = get_settings()
        broker = TokenBroker(s)
        try:
            path = lock_path()
            if path.exists():
                path.rename(path.with_suffix(".json.bak"))
            registry = ToolRegistry(s, broker)
            await registry.refresh(strict=False)
            if len(registry.cards) < 3:
                raise SystemExit("could not reach every MCP server; start them with `uv run mara dev` first")
            write_lock(registry.lock_entries())
            for server, cards in registry.cards.items():
                console.print(f"[green]{server}[/]: {', '.join(sorted(cards))}")
            backup = path.with_suffix(".json.bak")
            if backup.exists():
                backup.unlink()
            console.print(f"[bold]wrote {path}[/] (restart the orchestrator to load the new pins)")
        finally:
            await broker.aclose()

    arun(go())


# -------------------------------------------------------------------- dev
@app.command()
def dev(
    only: Annotated[str | None, typer.Option(help="Comma-separated service names to run")] = None,
    ui: Annotated[bool, typer.Option(help="Also run the web UI (needs Node.js)")] = False,
) -> None:
    """Run every service locally (no Docker). Ctrl+C stops them all."""
    from mara_cli.devrun import DevRunner

    DevRunner(only.split(",") if only else None, ui=ui).run()


# --------------------------------------------------------------- runs/ask
EVENT_STYLE = {
    "plan": "bold cyan",
    "task_started": "cyan",
    "task_finished": "green",
    "tool_call": "dim",
    "verification": "magenta",
    "approval_required": "bold yellow",
    "action_executed": "bold green",
    "action_failed": "bold red",
    "error": "bold red",
}


def _event_line(event: dict) -> str | None:
    t, d = event["type"], event["data"]
    if t == "plan":
        lines = [f"plan{' (replan)' if d.get('replan') else ''}:"]
        for task in d.get("tasks", []):
            deps = f" after {', '.join(task['depends_on'])}" if task.get("depends_on") else ""
            lines.append(f"  {task['id']} [{task['kind']}]{deps}: {task['goal']}")
        for a in d.get("assumptions", []):
            lines.append(f"  assumption: {a}")
        return "\n".join(lines)
    if t == "task_started":
        return f"→ {d['task_id']} {d['kind']} started"
    if t == "tool_call":
        return f"    {d['task_id']} {d['server']}.{d['tool']} {'ok' if d['ok'] else 'ERROR'} {d['latency_ms']}ms"
    if t == "task_finished":
        return f"✓ {d['task_id']} {d['status']} ({d['evidence']} evidence, {d['seconds']}s)" + (
            f"  gaps: {'; '.join(d['gaps'])}" if d.get("gaps") else ""
        )
    if t == "verification":
        return "verification " + ("passed" if d["passed"] else "failed: " + "; ".join(d.get("issues", [])[:3]))
    if t == "a2a_state":
        return f"    {d['task_id']} research agent: {d['state']}"
    if t == "approval_required":
        return f"approval required for {len(d['actions'])} action(s)"
    if t in ("action_executed", "action_failed", "action_decided"):
        return f"{t.replace('_', ' ')}: {d.get('action_id')} {d.get('status', '')} {d.get('error') or ''}".rstrip()
    if t == "error":
        return f"error: {d.get('message')}"
    return None


def _print_final(final: dict, run_id: str) -> None:
    console.print(
        Panel(
            Markdown(final.get("answer") or ""),
            title=f"answer · {final.get('status')}",
            subtitle=run_id,
            border_style="green",
        )
    )
    for note in final.get("notes") or []:
        console.print(Text.assemble(("note: ", "yellow"), note))
    if final.get("citations"):
        table = Table("id", "source", "title", box=None)
        for c in final["citations"]:
            table.add_row(c["id"], c["source"], Text(c["title"][:80] + (" ⚠" if c.get("flagged") else "")))
        console.print(table)
    for a in final.get("actions") or []:
        reason = f" ({a['policy_reason']})" if a.get("policy_reason") else ""
        console.print(Text.assemble(("action ", "bold"), f"{a['id']} {a['tool']} → {a['status']}{reason}"))
    cost = final.get("cost") or {}
    console.print(
        f"[dim]cost ${cost.get('total_usd', 0):.4f} · {cost.get('llm_calls', 0)} model calls · "
        f"{cost.get('seconds', 0)}s · prompts {', '.join((final.get('prompt_versions') or {}).values())}[/]"
    )


async def _follow(client, token: str, run_id: str, approve_as: str | None, reject: bool, quiet: bool) -> dict:
    last = 0
    while True:
        async for event in client.events(token, run_id, after=last):
            last = event["id"] or last
            if not quiet and (line := _event_line(event)):
                console.print(Text(line, style=EVENT_STYLE.get(event["type"], "")))
            if event["type"] == "approval_required":
                for a in event["data"]["actions"]:
                    console.print(
                        Panel(
                            Text(a["preview"]),
                            title=f"{a['id']} · {a['tool']} · risk {a['risk']}",
                            border_style="yellow",
                        )
                    )
                if not approve_as:
                    console.print(
                        f"Approve with: [bold]uv run mara approve <action-id> --user bob[/]   "
                        f"(then: uv run mara show {run_id})"
                    )
                    return await client.run(token, run_id)
                approver = await client.token(approve_as)
                for a in event["data"]["actions"]:
                    result = await client.decide(approver, a["id"], approve=not reject, note="via mara ask")
                    console.print(
                        f"{'rejected' if reject else 'approved'} {a['id']} as {approve_as}: {result['status']}"
                    )
            if event["type"] == "run_finished":
                return await client.run(token, run_id)
        run = await client.run(token, run_id)
        if run["status"] not in ("queued", "running", "executing", "awaiting_approval"):
            return run


@app.command()
def ask(
    question: str,
    user: Annotated[str, typer.Option(help="Dev user: alice, bob, dana, admin")] = "alice",
    approve_as: Annotated[str | None, typer.Option(help="Approve proposed actions as this user")] = None,
    reject: Annotated[bool, typer.Option(help="With --approve-as: reject instead")] = False,
    as_of: Annotated[str | None, typer.Option(help="Treat this date as today")] = None,
    no_actions: Annotated[bool, typer.Option(help="Never propose write actions")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Print the final run as JSON")] = False,
) -> None:
    """Ask a question and follow the run live."""
    from mara_core.api_client import MaraClient

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            token = await client.token(user)
            run_id = await client.ask(token, question, as_of=as_of, allow_actions=not no_actions)
            if not json_out:
                console.print(f"[dim]{run_id} · asking as {user}[/]")
            run = await _follow(client, token, run_id, approve_as, reject, quiet=json_out)
            if json_out:
                console.print_json(data=run)
            elif run.get("final"):
                _print_final(run["final"], run_id)
            else:
                console.print(f"run is {run['status']}")
        finally:
            await client.aclose()

    arun(go())


@app.command()
def approvals(user: Annotated[str, typer.Option(help="An approver, e.g. bob")] = "bob") -> None:
    """List actions waiting for approval."""
    from mara_core.api_client import MaraClient

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            items = await client.approvals(await client.token(user))
            if not items:
                console.print("nothing to approve")
            for a in items:
                console.print(
                    Panel(
                        Text(a["preview"]),
                        title=f"{a['id']} · {a['tool']} · risk {a['risk']} · asked by {a['username']}",
                        subtitle=Text(a["question"][:100]),
                        border_style="yellow",
                    )
                )
        finally:
            await client.aclose()

    arun(go())


@app.command()
def approve(
    action_id: str,
    user: Annotated[str, typer.Option()] = "bob",
    reject: Annotated[bool, typer.Option(help="Reject instead of approve")] = False,
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Approve (or reject) one proposed action."""
    from mara_core.api_client import MaraClient

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            result = await client.decide(await client.token(user), action_id, approve=not reject, note=note)
            console.print_json(data=result)
        finally:
            await client.aclose()

    arun(go())


@app.command()
def runs(user: Annotated[str, typer.Option()] = "alice", limit: int = 20) -> None:
    """List recent runs."""
    from mara_core.api_client import MaraClient

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            table = Table("run", "status", "cost", "question", box=None)
            for r in await client.runs(await client.token(user), limit):
                table.add_row(r["id"], r["status"], f"${float(r['cost_usd']):.4f}", Text(r["question"][:70]))
            console.print(table)
        finally:
            await client.aclose()

    arun(go())


@app.command()
def show(
    run_id: str,
    user: Annotated[str, typer.Option()] = "alice",
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a run's final answer, actions and cost."""
    from mara_core.api_client import MaraClient

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            run = await client.run(await client.token(user), run_id)
            if json_out:
                console.print_json(data=run)
            elif run.get("final"):
                _print_final(run["final"], run_id)
                for a in run.get("actions", []):
                    console.print(Text(f"{a['id']} {a['status']} {json.dumps(a.get('result'))[:120]}", style="dim"))
            else:
                console.print(f"run is {run['status']}")
        finally:
            await client.aclose()

    arun(go())


@app.command()
def replay(run_id: str, user: Annotated[str, typer.Option()] = "alice") -> None:
    """Re-run a finished run against its recorded tool results and compare the answers."""
    from mara_core.api_client import MaraClient
    from mara_evals.replay import diff_runs

    async def go() -> None:
        client = MaraClient(get_settings())
        try:
            token = await client.token(user)
            new_id = await client.replay(token, run_id)
            console.print(f"[dim]replaying {run_id} as {new_id}[/]")
            await _follow(client, token, new_id, None, False, quiet=True)
            report = await diff_runs(client, token, run_id, new_id)
            console.print_json(data=report)
        finally:
            await client.aclose()

    arun(go())


# ------------------------------------------------------------------ evals
@eval_app.command("generate")
def eval_generate() -> None:
    """Write the eval datasets (evals/datasets/*.jsonl) from the seeded world."""
    from mara_evals.datasets import write_all

    for name, count in write_all().items():
        console.print(f"[green]{name}[/]: {count} cases")


@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Argument(help="adversarial | golden | sql | all")] = "adversarial",
    limit: Annotated[int | None, typer.Option(help="Only the first N cases")] = None,
    concurrency: Annotated[int, typer.Option(help="Cases in flight at once")] = 4,
    category: Annotated[str | None, typer.Option(help="Only this category")] = None,
    judge: Annotated[bool, typer.Option(help="Also score with an LLM judge (needs a key)")] = False,
) -> None:
    """Run an end-to-end eval suite against the running platform."""
    from mara_evals.runner import run_suites

    suites = ["adversarial", "golden", "sql"] if suite == "all" else [suite]
    arun(run_suites(suites, limit=limit, concurrency=concurrency, category=category, judge=judge))


@eval_app.command("retrieval")
def eval_retrieval(k: Annotated[int, typer.Option(help="Cut-off for recall")] = 5) -> None:
    """Score retrieval alone (no models): recall@k and MRR over the retrieval dataset."""
    from mara_evals.retrieval import run_retrieval_eval

    arun(run_retrieval_eval(k))


# ------------------------------------------------------------------ audit
@audit_app.command("verify")
def audit_verify() -> None:
    """Check the audit log's hash chain."""
    from mara_core.audit import verify_chain
    from mara_core.db import make_pool

    async def go() -> None:
        pool = make_pool(get_settings().app_db_url)
        await pool.open()
        try:
            ok, bad = await verify_chain(pool)
            console.print("[bold green]audit chain intact[/]" if ok else f"[bold red]chain broken at entry {bad}[/]")
            if not ok:
                raise SystemExit(1)
        finally:
            await pool.close()

    arun(go())


def _env_hint() -> None:
    if not (repo_root() / ".env").exists() and not os.environ.get("MARA_QUIET"):
        console.print("[dim]tip: copy .env.example to .env to configure keys and models[/]", style="dim")


@app.callback()
def main_callback() -> None:
    _env_hint()


if __name__ == "__main__":
    app()
