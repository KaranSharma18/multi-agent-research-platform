"""The A2A server: a signed Agent Card plus JSON-RPC routes behind token checks."""

from __future__ import annotations

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    ClientCredentialsOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.signing import create_agent_card_signer
from mara_core.auth.keys import generate_ed25519, key_id, load_private_key
from mara_core.auth.verify import AuthError, JWTVerifier, bearer_from_header
from mara_core.logging import get_logger
from mara_core.settings import Settings, get_settings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mara_research_agent.engines import choose_engine
from mara_research_agent.executor import ResearchExecutor

log = get_logger(__name__)
CARD_PATH = "/.well-known/agent-card.json"
OPEN_PATHS = {CARD_PATH, "/healthz"}


def build_card(settings: Settings, public_url: str) -> AgentCard:
    skill = AgentSkill(
        id="web_research",
        name="Web research brief",
        description="Answers one research question with 3 to 6 sources and a claim list.",
        tags=["research", "web"],
        input_modes=["text/plain", "application/json"],
        output_modes=["application/json", "text/markdown"],
    )
    oauth = SecurityScheme(
        oauth2_security_scheme=OAuth2SecurityScheme(
            flows=OAuthFlows(
                client_credentials=ClientCredentialsOAuthFlow(
                    token_url=settings.auth_token_url, scopes={"research:run": "Run research tasks"}
                )
            )
        )
    )
    return AgentCard(
        name="web-research-agent",
        description="Researches a question on the open web and returns a cited brief.",
        version="1.2.0",
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json", "text/markdown"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False, extended_agent_card=False),
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=public_url.rstrip("/") + "/", protocol_version="1.0")
        ],
        security_schemes={"oauth": oauth},
        security_requirements=[SecurityRequirement(schemes={"oauth": StringList(list=["research:run"])})],
        skills=[skill],
    )


class RequireToken:
    """Every call except the Agent Card needs a token for this agent with `research:run`."""

    def __init__(self, app: ASGIApp, verifier: JWTVerifier, resource: str):
        self.app, self.verifier, self.resource = app, verifier, resource

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        try:
            principal = await self.verifier.averify(
                bearer_from_header(headers.get("authorization")), audience=self.resource
            )
            if not principal.has_scope("research:run"):
                raise AuthError("missing scope research:run", status=403, error="insufficient_scope")
        except AuthError as exc:
            response = JSONResponse(
                {"error": exc.error, "error_description": str(exc)},
                status_code=exc.status,
                headers={"WWW-Authenticate": f'Bearer error="{exc.error}"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, public_url: str | None = None) -> ASGIApp:
    settings = settings or get_settings()
    public_url = public_url or settings.research_agent_url
    card = build_card(settings, public_url)
    private_path, _ = generate_ed25519(settings.agent_card_private_key_file, settings.agent_card_public_key_file)
    key = load_private_key(private_path)
    signer = create_agent_card_signer(
        signing_key=key, protected_header={"kid": key_id(key.public_key()), "alg": "EdDSA", "jku": None, "typ": "JOSE"}
    )
    card = signer(card)

    engine = choose_engine(settings)
    log.info("research_engine_selected", engine=engine.name)
    handler = DefaultRequestHandler(
        agent_executor=ResearchExecutor(engine), task_store=InMemoryTaskStore(), agent_card=card
    )

    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse(f"ok engine={engine.name}")

    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/"), Route("/healthz", healthz)]
    verifier = JWTVerifier(
        jwks_url=settings.auth_jwks_url,
        issuer=settings.auth_issuer,
        audience=settings.research_agent_resource,
        leeway=settings.auth_leeway_seconds,
    )
    return RequireToken(Starlette(routes=routes), verifier, settings.research_agent_resource)
