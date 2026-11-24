from __future__ import annotations

from mara_core.logging import configure_logging
from mara_core.serve import run_coroutine
from mara_core.settings import get_settings
from mara_core.telemetry import setup_telemetry


def main() -> None:
    settings = get_settings()
    configure_logging("executor", settings.log_level, settings.log_json)
    setup_telemetry("executor", settings.otel_exporter_otlp_endpoint)
    from mara_executor.worker import Executor

    run_coroutine(Executor(settings).run_forever(), selector_loop=True)


if __name__ == "__main__":
    main()
