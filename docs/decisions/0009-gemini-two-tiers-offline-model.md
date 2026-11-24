# 0009. Gemini in two tiers, plus an offline stand-in

**Context.** Most calls in a run are routine (worker steps, synthesis, proposals); a few are where mistakes
cost the most (planning, verification).

**Decision.**
- **Mid tier** (`MODEL_MID`, default `gemini-3.8-flash`, thinking level low): planner and verifier.
- **Small tier** (`MODEL_SMALL`, default `gemini-3.5-flash-lite`, thinking minimal): workers, synthesis,
  proposals. A SQL worker escalates to the mid tier after two failed queries.
- Each tier has a fallback model used after repeated rate-limit or server errors.
- Temperature is left at Gemini 3's default (Google advises against lowering it for these models).
- `LLM_PROVIDER=offline` swaps in a deterministic rule-based model, so the whole system (tokens, MCP, A2A,
  approvals, executor) runs in CI and on a fresh machine with no key and no cost.

**Why.** Spending the stronger model where judgment matters keeps cost per run around a few cents. Model
names are configuration, so moving to newer models is an `.env` change, and prices live in
`config/prices.yaml` next to them.

**Trade-off.** The offline model's answers are crude. Use it to test plumbing, never to judge quality; eval
numbers only mean something with real models.
