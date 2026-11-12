"""Structured logging.

Logs are emitted as single-line JSON in CI (``TRENDPULSE_LOG_FORMAT=json``)
and as readable text locally. A context variable carries the current
``run_id`` and ``stage`` so every log line is attributable to an execution.
Secret-looking values are redacted before they reach any handler.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar("stage", default=None)

_REDACT_PATTERNS = [
    re.compile(r"(key=)[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(AIza)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)"),
]


def redact(text: str) -> str:
    for pat in _REDACT_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + "***" + m.group(2), text)
        else:
            text = pat.sub(lambda m: m.group(1) + "***", text)
    return text


def set_context(run_id: str | None = None, stage: str | None = None) -> None:
    if run_id is not None:
        _run_id.set(run_id)
    _stage.set(stage)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        record.stage = _stage.get()
        try:
            record.msg = redact(record.getMessage())
            record.args = None
        except Exception:  # noqa: BLE001, S110 - never break logging
            pass
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", None),
            "stage": getattr(record, "stage", None),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = ""
        if getattr(record, "run_id", None):
            ctx = f" [{record.run_id[:8]}{'/' + record.stage if getattr(record, 'stage', None) else ''}]"
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7}{ctx} {record.name}: {record.getMessage()}"
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


class _StdoutHandler(logging.StreamHandler):
    """Always writes to the *current* sys.stdout (survives pytest/streamlit stream swaps)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


_configured = False


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    global _configured
    if _configured:
        return
    level = (level or os.environ.get("TRENDPULSE_LOG_LEVEL") or "INFO").upper()
    fmt = fmt or os.environ.get("TRENDPULSE_LOG_FORMAT") or ("json" if os.environ.get("CI") else "text")
    handler = _StdoutHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    root = logging.getLogger("trendpulse")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trendpulse.{name}" if not name.startswith("trendpulse") else name)
