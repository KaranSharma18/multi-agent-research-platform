"""Dynamic tool loading for workers.

A worker starts with its core tools (config/scopes.yaml) plus `find_tools`.
When it needs something else it calls `find_tools("what I need")`, which
searches the pinned registry, but only within the task's scopes, and returns up
to three tool names. `ToolLoaderMiddleware` then shows those tools' schemas on
the next model call.

Every candidate tool is registered with `create_agent` up front and the
middleware filters what the model sees per call (`request.override(tools=...)`),
which is LangChain's documented pattern for per-call tool selection. Scope is
enforced three times: here (only in-scope tools are registered or found), in
the MCP server's ScopeGuard, and inside each tool.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mara_core.config import scope_policy
from pydantic import BaseModel, Field

from mara_orchestrator.llm.cost import RunBudget
from mara_orchestrator.tools.interceptors import TaskToolInterceptor
from mara_orchestrator.tools.registry import ToolRegistry

MAX_VISIBLE = 8
TIMEOUTS = {"knowledge": 5.0, "warehouse": 8.0, "live_data": 8.0}


class FindToolsInput(BaseModel):
    need: str = Field(description="What you need to do, e.g. 'timeline of one incident' or 'exchange rate'")


def make_find_tools(registry: ToolRegistry, server: str, scopes: list[str], visible: set[str]) -> StructuredTool:
    def find_tools(need: str) -> tuple[str, dict[str, Any]]:
        cards = registry.search(need, server, scopes, k=3, exclude=visible)
        if not cards:
            return "No additional tools match that need within this task's permissions.", {"tools": []}
        lines = [f"- {c.name}: {c.description.splitlines()[0][:160]}" for c in cards]
        return "These tools are now available:\n" + "\n".join(lines), {"tools": [c.name for c in cards]}

    return StructuredTool.from_function(
        func=find_tools,
        name="find_tools",
        description="Search for more tools you are allowed to use. Matching tools become available on your next step.",
        args_schema=FindToolsInput,
        response_format="content_and_artifact",
    )


def build_task_tools(
    *,
    registry: ToolRegistry,
    server: str,
    kind: str,
    scopes: list[str],
    url: str,
    token: str,
    interceptor: TaskToolInterceptor,
) -> tuple[list, set[str], set[str]]:
    """LangChain tools for one task, wired to its own token. Returns (tools, core names, registry names)."""
    cards = registry.cards_for(server, scopes)
    timeout = TIMEOUTS.get(server, 8.0)
    # The MCP client uses sse_read_timeout as the HTTP read timeout, so both must be set.
    connection = {
        "transport": "streamable_http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": timeout,
        "sse_read_timeout": timeout,
    }
    tools = [
        convert_mcp_tool_to_langchain_tool(
            None,
            card.definition,
            connection=connection,
            server_name=server,
            tool_interceptors=[interceptor],
            handle_tool_errors=True,
        )
        for card in cards
    ]
    core = set(scope_policy().core_tools.get(kind, [])) & {c.name for c in cards}
    registry_names = {c.name for c in cards}
    tools.append(make_find_tools(registry, server, scopes, core))
    return tools, core, registry_names


def _name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return tool.get("name") or (tool.get("function") or {}).get("name")
    return getattr(tool, "name", None)


def loaded_tools(messages: list) -> list[str]:
    names: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.name == "find_tools" and isinstance(msg.artifact, dict):
            for name in msg.artifact.get("tools", []):
                if name not in names:
                    names.append(name)
    return names


class ToolLoaderMiddleware(AgentMiddleware):
    """Show the model only its core tools plus whatever `find_tools` loaded."""

    def __init__(self, core: set[str], registry_names: set[str], max_visible: int = MAX_VISIBLE):
        super().__init__()
        self.core = core
        self.registry_names = registry_names
        self.max_visible = max_visible

    def _filter(self, request: ModelRequest) -> ModelRequest:
        wanted = [n for n in self.core] + [n for n in loaded_tools(request.messages) if n not in self.core]
        wanted = wanted[: self.max_visible]
        tools = [t for t in request.tools if _name(t) not in self.registry_names or _name(t) in wanted]
        return request.override(tools=tools)

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._filter(request))

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._filter(request))


class EscalateAfterErrorsMiddleware(AgentMiddleware):
    """Swap to the mid-tier model after repeated errors from one tool (the SQL Analyst's run_sql)."""

    def __init__(self, tool: str, model: Any, threshold: int = 2):
        super().__init__()
        self.tool, self.model, self.threshold = tool, model, threshold

    async def awrap_model_call(self, request: ModelRequest, handler):
        failures = sum(
            1 for m in request.messages if isinstance(m, ToolMessage) and m.name == self.tool and m.status == "error"
        )
        if self.model is not None and failures >= self.threshold:
            request = request.override(model=self.model)
        return await handler(request)


class ToolBudgetMiddleware(AgentMiddleware):
    """Cap a worker's real tool calls (MCP tools), without counting `find_tools` or `WorkerAnswer`.

    Over the cap, extra calls get an error result telling the model to report
    what it has. If it keeps calling tools anyway, the loop ends.
    """

    def __init__(self, limit: int, counted: set[str]):
        super().__init__()
        self.limit = limit
        self.counted = counted

    def _check(self, state: dict) -> dict | None:
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return None
        used = sum(
            1 for m in messages[:-1] if isinstance(m, AIMessage) for tc in m.tool_calls if tc["name"] in self.counted
        )
        over = []
        for tc in last.tool_calls:
            if tc["name"] in self.counted:
                used += 1
                if used > self.limit:
                    over.append(tc)
        if not over:
            return None
        blocked = [
            ToolMessage(
                content=f"Tool-call limit reached ({self.limit}). Do not call more tools; call "
                "WorkerAnswer now with what you found and list anything missing in gaps.",
                tool_call_id=tc["id"],
                name=tc["name"],
                status="error",
            )
            for tc in over
        ]
        if used > self.limit + 2:  # ignored the instruction twice: stop the loop
            return {"messages": blocked, "jump_to": "end"}
        return {"messages": blocked}

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        return self._check(state)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        return self._check(state)


class BudgetMiddleware(AgentMiddleware):
    """Stop a worker's loop when the run's budget is spent."""

    def __init__(self, budget: RunBudget):
        super().__init__()
        self.budget = budget

    async def awrap_model_call(self, request: ModelRequest, handler):
        self.budget.check()
        return await handler(request)


def last_ai_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content.strip():
            return msg.content
    return ""
