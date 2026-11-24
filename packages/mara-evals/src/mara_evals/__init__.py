"""MARA evals: datasets generated from the seeded world, code scorers, a naive
simulated approver, Braintrust experiments and replay diffs.

    uv run mara eval generate          # write datasets
    uv run mara eval run adversarial   # 120 attack and failure cases, end to end
    uv run mara eval run golden        # 60 ordinary questions
    uv run mara eval run sql           # 80 exact-number questions
    uv run mara eval retrieval         # recall@5 and MRR, no models
"""
