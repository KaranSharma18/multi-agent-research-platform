"""The run graph.

    guard ─► plan ─► schedule ─┬─► worker_docs ───────┐
              ▲        ▲       ├─► worker_sql ────────┤  (fan-out with Send,
              │        │       ├─► worker_live_api ───┤   one wave at a time)
              │        │       └─► worker_web_research┤
              │        └──────────────────────────────┘
              └── (follow-up requested, replans left)
    schedule ─► synthesize ─► verify ─┬─► synthesize (one revision)
                                      ├─► propose ─► approve (interrupt) ─► execute ─► finalize
                                      └─► finalize

Every path ends in `finalize`, which records the outcome. See docs/architecture.md.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from mara_orchestrator.graph.context import RunContext
from mara_orchestrator.graph.nodes.actions import approve, execute, propose, route_after_propose
from mara_orchestrator.graph.nodes.answer import route_after_verify, synthesize, verify
from mara_orchestrator.graph.nodes.finalize import finalize
from mara_orchestrator.graph.nodes.guard import guard, route_after_guard
from mara_orchestrator.graph.nodes.planning import (
    plan,
    route_after_plan,
    route_after_schedule,
    schedule,
)
from mara_orchestrator.graph.nodes.workers import worker
from mara_orchestrator.graph.state import RunState, WorkerInput

WORKER_KINDS = ("docs", "sql", "live_api", "web_research")


def build_graph(checkpointer=None):
    g = StateGraph(RunState, context_schema=RunContext)
    g.add_node("guard", guard)
    g.add_node("plan", plan)
    g.add_node("schedule", schedule)
    workers = [f"worker_{kind}" for kind in WORKER_KINDS]
    for name in workers:  # one function, four nodes: separate names make traces readable
        g.add_node(name, worker, input_schema=WorkerInput)
    g.add_node("synthesize", synthesize)
    g.add_node("verify", verify)
    g.add_node("propose", propose)
    g.add_node("approve", approve)
    g.add_node("execute", execute)
    g.add_node("finalize", finalize)

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", route_after_guard, ["plan", "finalize"])
    g.add_conditional_edges("plan", route_after_plan, ["schedule", "finalize"])
    g.add_conditional_edges("schedule", route_after_schedule, [*workers, "plan", "synthesize"])
    for name in workers:
        g.add_edge(name, "schedule")
    g.add_edge("synthesize", "verify")
    g.add_conditional_edges("verify", route_after_verify, ["synthesize", "propose", "finalize"])
    g.add_conditional_edges("propose", route_after_propose, ["approve", "finalize"])
    g.add_edge("approve", "execute")
    g.add_edge("execute", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer, name="mara-run")
