# data/

Everything the platform generates at runtime lands in `data/generated/` (git-ignored):

| path | written by | what it is |
|---|---|---|
| `seed_meta.json` | `mara seed` | the seeded world's date, seed, counts, expected facts and canary values |
| `fixtures/` | `mara seed` | JSON the mock incident, status, GitHub and FX APIs serve |
| `corpus/` | `mara seed` | about 150 markdown documents (front matter carries ACL groups) |
| `qdrant/` | `mara ingest` | the vector index, when Qdrant runs in embedded mode |
| `sandbox/` | actions server | issues and Slack messages "sent" in sandbox mode |
| `actions_state.sqlite3` | actions server | idempotency keys and used approval token ids |
| `evals/` | `mara eval generate` | eval datasets (JSON Lines) |
| `eval_reports/` | `mara eval run` | eval results as JSON and Markdown |

Delete the folder and run `uv run mara seed && uv run mara ingest` to start fresh.
Set `MARA_DATA_DIR` to put it somewhere else.
