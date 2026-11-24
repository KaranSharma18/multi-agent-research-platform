from __future__ import annotations

import os

from mara_core.logging import configure_logging
from mara_core.serve import run
from mara_core.settings import get_settings
from mara_core.telemetry import setup_telemetry


def main() -> None:
    settings = get_settings()
    configure_logging("web-research-agent", settings.log_level, settings.log_json)
    setup_telemetry("web-research-agent", settings.otel_exporter_otlp_endpoint)
    from mara_research_agent.server import create_app

    # selector_loop=False: the Claude Agent SDK starts a subprocess, which needs
    # the default (Proactor) event loop on Windows.
    run(create_app(settings), host=settings.host, port=int(os.environ.get("PORT", "8200")), selector_loop=False)


if __name__ == "__main__":
    main()
