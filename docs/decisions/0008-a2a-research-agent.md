# 0008. Web research is a remote agent behind A2A

**Context.** Open-web research is a different kind of work from internal lookups: many fetches, flaky pages,
its own budget, and a lot of untrusted text. It's also the kind of capability another team (or vendor) would
own.

**Decision.** A separate service, the web research agent, speaks the A2A protocol (a2a-sdk). It publishes a
signed Agent Card and accepts tasks with a client-credentials token for `research:run`. Inside, it runs one
of three engines:

- **claude**: the Claude Agent SDK, restricted to `WebSearch` and `WebFetch` (`tools` and
  `allowed_tools`), `dontAsk` permission mode so anything else is denied, a `PreToolUse` hook enforcing
  the domain policy, `max_turns` and `max_budget_usd` caps, and a JSON-schema structured output.
  This needs an Anthropic API key (or Bedrock/Vertex), because the SDK runs Claude models.
- **gemini**: Gemini with Google Search grounding, for setups with only a Gemini key; sources come from
  grounding metadata, not from the model's text.
- **stub**: scripted behaviours (complete, fail, stall, input-required, inject) for CI and evals.

The orchestrator pins the card's signing key, refuses a card whose endpoint isn't the configured one, sends
no user identity, treats the brief as untrusted evidence, and adds the agent's reported spend to the run's
cost.

**Why.** An opaque, independently deployed agent with a clear contract (task states, artifacts, cost)
is exactly what A2A is for, and it keeps a whole class of risk out of the orchestrator.

**Trade-off.** Another service and protocol to run. The stub engine keeps CI and offline runs simple.
