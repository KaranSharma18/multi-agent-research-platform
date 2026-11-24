"""`python -m mara_token_service` or `mara-token-service`."""

from __future__ import annotations

import os

from mara_core.logging import configure_logging
from mara_core.serve import run
from mara_core.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging("token-service", settings.log_level, settings.log_json)
    from mara_token_service.app import create_app

    run(create_app(settings), host=settings.host, port=int(os.environ.get("PORT", "8090")))


if __name__ == "__main__":
    main()
