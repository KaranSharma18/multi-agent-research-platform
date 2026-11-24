"""Run an ASGI app with uvicorn, on Linux, macOS and Windows alike.

Windows needs one special case. Psycopg's async mode cannot use the default
Windows event loop (Proactor), so services that talk to Postgres run on a
SelectorEventLoop there. The research agent is the exception: the Claude Agent
SDK starts a subprocess, which on Windows needs the Proactor loop, so it passes
`selector_loop=False`.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn


def run(app, *, host: str, port: int, selector_loop: bool = True, log_level: str = "info") -> None:
    config = uvicorn.Config(
        app, host=host, port=port, log_level=log_level.lower(), proxy_headers=True, access_log=False
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32" and selector_loop:
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


def run_coroutine(coro, *, selector_loop: bool = True):
    """Run a top-level coroutine (CLI commands, workers) with the right loop on Windows."""
    if sys.platform == "win32" and selector_loop:
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
