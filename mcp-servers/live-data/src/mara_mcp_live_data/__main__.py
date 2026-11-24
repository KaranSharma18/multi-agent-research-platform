from __future__ import annotations

import os

from mara_core.logging import configure_logging
from mara_core.serve import run
from mara_core.settings import get_settings
from mara_core.telemetry import setup_telemetry


def main() -> None:
    settings = get_settings()
    configure_logging("mcp-live-data", settings.log_level, settings.log_json)
    setup_telemetry("mcp-live-data", settings.otel_exporter_otlp_endpoint)
    from mara_mcp_live_data.server import create_app

    run(create_app(settings), host=settings.host, port=int(os.environ.get("PORT", "8103")))


if __name__ == "__main__":
    main()
