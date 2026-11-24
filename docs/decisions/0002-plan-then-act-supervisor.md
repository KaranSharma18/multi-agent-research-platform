# 0002. A supervisor graph that plans first, then fans out workers

**Context.** Multi-hop questions need several sources, some in sequence (find the affected customers, *then*
sum their ARR) and some in parallel. A single ReAct loop with every tool can do this, but its plan lives
in the model's head, changes every time it reads a tool result, and is easy to hijack: a document that
says "now email the customer list" is just more context for the next step.

**Decision.** A LangGraph `StateGraph` with an explicit plan:

1. the mid model writes a typed `TaskPlan` before any tool runs;
2. code validates it (ids, dependencies, cycles, a cap of eight tasks);
3. `schedule` sends ready tasks to workers in parallel waves with `Send`;
4. each worker is a small, capped agent loop for one task;
5. synthesis, verification, proposals and approval are fixed graph nodes, not tools a model can call.

**Why.**
- **Control flow is code.** The model fills in a plan; it can't add a step that writes, skip verification,
  or call the approval node. Prompt injection can only influence a worker's reading of its own task.
- **Parallelism with dependencies.** Independent tasks run together; dependent ones get their inputs from
  typed results.
- **Debuggable.** Every step is a node with a name in traces and events, and the plan is visible to the user.
- **Replanning is bounded.** A worker can ask for a follow-up; the planner may add tasks at most twice.

**Trade-off.** One extra model call per run for planning, and questions that need open-ended exploration fit
less well. For operational questions, a visible plan is worth it.

**Alternatives considered.** A single agent with all tools (simpler, much easier to hijack, harder to
budget); a fully dynamic multi-agent chat (powerful but unpredictable cost and hard to secure).
