from __future__ import annotations

import os

from mara_core.logging import configure_logging
from mara_core.serve import run
from mara_core.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging("mock-apis", settings.log_level, settings.log_json)
    from mara_mock_apis.app import create_app

    run(create_app(), host=settings.host, port=int(os.environ.get("PORT", "8300")))


if __name__ == "__main__":
    main()
