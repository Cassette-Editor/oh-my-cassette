from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_LOGGER_NAME = "oh_my_cassette.web_demo"


def web_log_dir() -> Path:
    configured = str(os.getenv("OMC_WEB_LOG_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "web_demo" / "logs").resolve()


def web_log_path() -> Path:
    return web_log_dir() / "web_demo.log"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(name) or "").strip() or default))
    except ValueError:
        return default


def _logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if getattr(logger, "_omc_web_configured", False):
        return logger
    path = web_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=_int_env("OMC_WEB_LOG_MAX_BYTES", 5_000_000),
        backupCount=_int_env("OMC_WEB_LOG_BACKUPS", 3),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._omc_web_configured = True  # type: ignore[attr-defined]
    return logger


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _clean_value(v) for k, v in value.items() if "key" not in str(k).lower()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item) for item in value[:20]]
    return str(value)[:500]


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event}
    payload.update({key: _clean_value(value) for key, value in fields.items()})
    try:
        _logger().info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        pass
