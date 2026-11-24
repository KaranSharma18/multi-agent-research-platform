"""Shared building blocks for every MARA service.

Nothing in here knows about LangGraph or any one service. It holds the pieces
several services need to agree on: settings, token handling, PII masking,
prompt-injection heuristics, retrieval encoders and database helpers.
"""

__version__ = "0.1.0"
