from __future__ import annotations

import logging

from jyd_probe.logging_config import configure_file_logging, log_event, redact_text


def test_redact_text_masks_json_tokens_and_complete_cookie_headers() -> None:
    redacted = redact_text(
        'access_token="secret" Cookie: session=abc; theme=dark\n'
        'refresh_token="refresh-secret"'
    )
    assert "secret" not in redacted
    assert "session=abc" not in redacted
    assert "theme=dark" not in redacted
    assert "refresh-secret" not in redacted


def test_file_logging_writes_structured_event_and_redacts_secrets(tmp_path) -> None:
    logger_name = "jyd_probe.test.logging"
    logger = logging.getLogger(logger_name)
    original_handlers = list(logger.handlers)
    original_propagate = logger.propagate
    try:
        logger.handlers.clear()
        path = configure_file_logging(
            tmp_path,
            "workbench.log",
            logger_name=logger_name,
            propagate=False,
        )
        log_event(
            logger,
            "workbench.test",
            "测试日志",
            component="workbench",
            correlation_id="corr-123",
            detail="api_key=secret-value&token=another-secret",
        )
        for handler in logger.handlers:
            handler.flush()

        content = path.read_text(encoding="utf-8")
        assert "[EVENT workbench.test] 测试日志" in content
        assert '"correlation_id":"corr-123"' in content
        assert "secret-value" not in content
        assert "another-secret" not in content
        assert "api_key=***" in content
        assert "token=***" in content
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = original_handlers
        logger.propagate = original_propagate


def test_file_logging_configuration_is_idempotent(tmp_path) -> None:
    logger_name = "jyd_probe.test.idempotent"
    logger = logging.getLogger(logger_name)
    original_handlers = list(logger.handlers)
    original_propagate = logger.propagate
    try:
        logger.handlers.clear()
        configure_file_logging(tmp_path, "agent.log", logger_name=logger_name)
        configure_file_logging(tmp_path, "agent.log", logger_name=logger_name)
        assert len(logger.handlers) == 1
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = original_handlers
        logger.propagate = original_propagate
