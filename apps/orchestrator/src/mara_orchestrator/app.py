"""The orchestrator service: FastAPI app, wiring and startup."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mara_core.auth.approval import ApprovalSigner
from mara_core.auth.keys import generate_ed25519
from mara_core.auth.verify import JWTVerifier
from mara_core.db import make_pool, migrate
from mara_core.logging import get_logger
from mara_core.paths import resolve
from mara_core.settings import Settings, get_settings
from mara_core.telemetry import instrument_fastapi
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mara_orchestrator.a2a.client import ResearchClient
from mara_orchestrator.api import approvals, runs
from mara_orchestrator.auth.broker import TokenBroker
from mara_orchestrator.db import MIGRATIONS
from mara_orchestrator.db.repo import Repo
from mara_orchestrator.events import EventBus
from mara_orchestrator.graph.build import build_graph
from mara_orchestrator.graph.context import Services
from mara_orchestrator.runner import Runner
from mara_orchestrator.tools.registry import ToolRegistry

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = make_pool(settings.app_db_url, max_size=20, name="orchestrator")
        await pool.open(wait=True, timeout=30)
        async with pool.connection() as conn:
            await migrate(conn, MIGRATIONS, namespace="orchestrator")
        checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await checkpointer.setup()

        # Dev convenience: create the approval key pair if it does not exist yet.
        if settings.environment != "prod":
            generate_ed25519(resolve(settings.approval_private_key_file), resolve(settings.approval_public_key_file))

        repo = Repo(pool)
        events = EventBus(repo, settings.app_db_url)
        await events.start()
        broker = TokenBroker(settings)
        registry = ToolRegistry(settings, broker)
        try:
            await registry.refresh()
        except Exception as exc:  # servers may still be starting; each run retries stale catalogs
            log.warning("tool_catalog_refresh_failed", error=str(exc)[:300])
        services = Services(
            settings=settings,
            repo=repo,
            events=events,
            broker=broker,
            registry=registry,
            research=ResearchClient(settings, broker),
        )
        graph = build_graph(checkpointer)
        runner = Runner(services, graph)
        app.state.settings = settings
        app.state.repo = repo
        app.state.events = events
        app.state.registry = registry
        app.state.runner = runner
        app.state.pool = pool
        app.state.verifier = JWTVerifier(
            jwks_url=settings.auth_jwks_url,
            issuer=settings.auth_issuer,
            audience=settings.orchestrator_resource,
            leeway=settings.auth_leeway_seconds,
        )
        app.state.approval_signer = ApprovalSigner(
            str(resolve(settings.approval_private_key_file)), settings.approval_token_ttl_seconds
        )
        await runner.recover()
        await runner.start_sweeper()
        log.info(
            "orchestrator_ready",
            llm_provider=settings.llm_provider,
            model_mid=settings.model_mid,
            model_small=settings.model_small,
        )
        try:
            yield
        finally:
            await runner.shutdown()
            await events.stop()
            await broker.aclose()
            await pool.close()

    app = FastAPI(
        title="MARA orchestrator",
        version="0.1.0",
        lifespan=lifespan,
        description="Multi-Agent Research & Action platform: ask, follow, approve.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    app.include_router(runs.router)
    app.include_router(approvals.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        checks = {}
        try:
            async with request.app.state.pool.connection() as conn:
                await conn.execute("select 1")
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"[:200]
        registry = request.app.state.registry
        checks["tool_catalogs"] = {server: len(cards) for server, cards in registry.cards.items()}
        ok = checks["database"] == "ok" and len(registry.cards) == 3
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    instrument_fastapi(app)
    return app
