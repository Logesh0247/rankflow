"""Logging tests (2026-11-21 startup logging bug).

Live incident: at every startup `main.py` logged
`logger.info("storage validation ok: %s", report["checks"])`
where `report["checks"]` is a dict — and the
`RedactSecretsFilter` replaced a dict argument with a tuple of
its KEYS, so `"%s" % (six keys)` raised
"TypeError: not all arguments converted during string
formatting" at emit time. The filter is the root cause (a dict
is a legitimate logging argument); main.py now formats the
checks into the message as well.
"""
import logging

from app.core.logging import RedactSecretsFilter


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _logger_with_filter():
    logger = logging.getLogger("rankflow.test-logging")
    logger.handlers = []
    logger.filters = []
    logger.addFilter(RedactSecretsFilter())
    logger.addHandler(_Capture())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def test_dict_argument_formats_without_type_error():
    """A dict passed as a single "%s" argument (the startup
    case) must format — the filter must not turn it into a
    tuple of its keys."""
    logger = _logger_with_filter()
    checks = {"storage_root_writable": True, "database_file_exists": True,
              "disk_free_bytes": 1234, "disk_free_ok": True,
              "rendered_videos_checked": 0, "rendered_videos_missing": 0}
    logger.info("storage validation ok: %s", checks)
    (record,) = logger.handlers[0].records
    assert record.getMessage() == f"storage validation ok: {checks}"


def test_tuple_argument_still_formats():
    logger = _logger_with_filter()
    logger.info("job %s stage %s", "job-1", "RESEARCH")
    (record,) = logger.handlers[0].records
    assert record.getMessage() == "job job-1 stage RESEARCH"


def test_bearer_token_in_string_arg_is_still_redacted():
    logger = _logger_with_filter()
    logger.info("Authorization: Bearer abc123def")
    (record,) = logger.handlers[0].records
    assert "abc123def" not in record.getMessage()
    assert "Bearer ***" in record.getMessage()


def test_dict_argument_values_are_redacted_not_keys():
    """A dict argument's string VALUES are redacted; its keys
    (the check names) are kept, so the message stays readable."""
    logger = _logger_with_filter()
    logger.info("detail: %s", {"token": "Bearer abc123def", "ok": True})
    (record,) = logger.handlers[0].records
    message = record.getMessage()
    assert "abc123def" not in message
    assert "token" in message and "ok" in message


def test_startup_logs_storage_validation_without_type_error():
    """Entering the app (lifespan) runs the storage-validation
    logging without raising — the exact call that crashed at
    every startup before the fix. Whether the line is the "ok"
    or the warning form depends on the machine (free disk, files
    on disk), so assert that a storage-validation line was
    emitted at all and that no record failed to format: before
    the fix, emitting the ok line raised TypeError inside the
    logging machinery."""
    import logging as _logging

    from fastapi.testclient import TestClient

    from main import app

    logger = _logging.getLogger("rankflow.startup")
    capture = _Capture()
    logger.addHandler(capture)
    logger.setLevel(_logging.INFO)
    try:
        with TestClient(app):
            pass
    finally:
        logger.removeHandler(capture)
    messages = [r.getMessage() for r in capture.records]
    assert any("storage validation" in m for m in messages), messages
