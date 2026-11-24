# 0013. Evals generated from the same seeded world as the data

**Context.** Hand-written eval answers go stale as soon as the data changes, and relative questions ("last
week") have a different right answer every day.

**Decision.** `mara seed` builds a deterministic world from a seed and an "as of" date: customers, invoices,
incidents and their impacts, usage, GitHub issues, and about 150 documents, including planted injections,
canary personal data and restricted documents. The eval generator builds cases from the same world, so every
expected number is computed, not typed. Suites: adversarial (120), golden (60), SQL (80), retrieval (150).

Scoring is code first: completion (status, facts, expected tools and actions) and safety (no canaries, no
PII, no forbidden targets, no accepted write SQL, budget) are separate, because they fail differently. A
naive simulated approver approves everything it's asked to in action cases, which tests whether the
automated defenses hold without the human. Results go to JSON and Markdown reports and, with a key, to
Braintrust. An optional LLM judge (autoevals Factuality on Gemini) adds a second opinion, and agentevals
checks tool trajectories.

Replay re-runs a finished run with every tool result served from its recording, so a prompt or model change
can be compared on identical inputs.

**Trade-off.** A synthetic world is smaller and cleaner than a real one. It makes the evals exact and
repeatable, which matters more for catching regressions.
