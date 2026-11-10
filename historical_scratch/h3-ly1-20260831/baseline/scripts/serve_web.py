from __future__ import annotations

import logging

import uvicorn

from app.services.logging_config import configure_logging, log_event


logger = logging.getLogger(__name__)


def main() -> None:
    """Run the local Web service with the same rotating file log as workers."""

    configure_logging("web")
    log_event(
        logger,
        "web.starting",
        "Web 服务正在启动",
        host="127.0.0.1",
        port=8000,
    )
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
