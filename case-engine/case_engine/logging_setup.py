"""Centralised structured logging for the C.A.S.E. engine.

Every engine module obtains its logger through :func:`get_logger`, so
the whole engine logs under a single ``case_engine.`` namespace that
host applications can route, silence or reformat without touching the
stdlib root logger.  This module itself deliberately performs **no**
logging from its formatters (a formatter that logs could recurse into
itself); only :func:`configure_logging` emits a single DEBUG record
after installation.

The engine emits three kinds of log events:

* ``DEBUG``  - function entry/exit, state transitions, LLM request payloads.
* ``INFO``   - session lifecycle, node completions, audit appends, synthesis.
* ``WARNING/ERROR`` - validation rejections, adapter fallbacks, ledger issues.

Every logger is namespaced under ``case_engine.`` so applications can
route engine logs independently:

>>> logging.getLogger("case_engine.pipeline").setLevel(logging.DEBUG)

Contents
--------
- :func:`get_logger` — namespace-bound logger factory.
- :class:`ConsoleFormatter` — human-readable, single line.
- :class:`JsonFormatter` — one JSON object per line (for ship-to-Loki/
  CloudWatch pipelines), including an ``extra`` merge of structured keys
  such as ``session_id``, ``node`` and ``model_id``.
- :func:`configure_logging` — idempotent one-call setup of the engine
  root logger (console + optional file handler).

Invariants
----------
- :func:`configure_logging` is idempotent and *replaces* only handlers
  on the ``case_engine`` root logger; handlers installed by other code
  (on other loggers) are untouched, and propagation stays disabled so
  engine records never leak into application logs uninvited.
- Log lines remain single-line-parseable: the JSON formatter emits
  exactly one object per record, with tracebacks carried in a dedicated
  ``exception`` field rather than inline.
- Secrets (API keys) and full prompt payloads must never reach this
  transport above DEBUG; masking discipline lives at the emitting call
  sites, this module guarantees only faithful rendering.

See also: docs/ARCHITECTURE.md, case_engine/config.py (``log_level`` /
``log_json`` options), case_engine/engine.py (library-courteous,
lazy logging setup).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

__all__ = ["get_logger", "configure_logging", "JsonFormatter", "ConsoleFormatter"]

_ROOT_LOGGER = "case_engine"


def get_logger(name: str) -> logging.Logger:
    """Return a logger namespaced under the engine root logger.

    All engine modules call this once at module top with ``__name__``;
    embedders may use it too to participate in the same namespace.

    Args:
        name: Module ``__name__`` or any suffix.  Names not starting with
            ``case_engine`` are placed under the ``case_engine.`` namespace
            automatically.

    Returns:
        A standard :class:`logging.Logger` instance (no handlers are
        installed here — configuration happens once via
        :func:`configure_logging`).

    Example:
        >>> log = get_logger("pipeline.nodes")
        >>> log.name
        'case_engine.pipeline.nodes'

    Structured emission (shown, not executed — the rendered output
    depends on which handlers the host application has installed):

        >>> log.info("node completed", extra={"node": "node_1", "session_id": "s-42"})  # doctest: +SKIP
    """
    if name.startswith(_ROOT_LOGGER):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER}.{name}")


# Module logger (declared after get_logger so the namespace helper exists).
log = get_logger(__name__)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter: ``time LEVEL [logger] message (extras)``."""

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a compact console line.

        Structured ``extra`` keys (anything not a stdlib LogRecord
        attribute) are appended as ``k=v`` pairs inside parentheses;
        when ``exc_info`` is set the formatted traceback is appended
        after a newline.  This method never logs — a logging formatter
        that logs could recurse into itself.

        Args:
            record: The log record to render.

        Returns:
            Single-line string; structured extras are appended as ``k=v`` pairs.
        """
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        base = f"{ts} {record.levelname:<7} [{record.name}] {record.getMessage()}"
        known = set(logging.LogRecord("", 0, "", 0, None, None, None).__dict__) | {
            "message", "asctime",
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in known and not k.startswith("_")}
        if extras:
            base += " (" + " ".join(f"{k}={v}" for k, v in extras.items()) + ")"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """One-JSON-object-per-line formatter for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a JSON line.

        Structured ``extra`` keys are merged at the top level next to the
        standard fields, so log shippers can index ``session_id`` /
        ``node`` / ``model_id`` directly.  Non-serialisable extra values
        fall back to ``str()`` via ``default=str`` (nothing raises).
        This method never logs — a logging formatter that logs could
        recurse into itself.

        Args:
            record: The log record to render.

        Returns:
            JSON string with ``ts``, ``level``, ``logger``, ``message`` and
            any structured extras merged at the top level; a formatted
            traceback rides in the dedicated ``exception`` field.
        """
        known = set(logging.LogRecord("", 0, "", 0, None, None, None).__dict__) | {
            "message", "asctime",
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in known and not k.startswith("_")}
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **extras,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str | int = "INFO",
    json_mode: bool = False,
    log_file: str | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure the engine root logger once, idempotently.

    Safe to call multiple times: existing engine handlers are replaced,
    handlers installed by other code are left untouched.  Applications
    embedding the engine may call it with their own ``stream``; the
    library itself calls it lazily (see
    :meth:`case_engine.engine.CASEEngine.__init__`) only when no engine
    handler exists yet.

    Side effects: mutates the ``case_engine`` logger (level, handlers,
    ``propagate=False``) and opens ``log_file`` when given; emits one
    DEBUG record after installation.  It performs no other logging.

    Args:
        level: Log level name or number (default ``"INFO"``).
        json_mode: Emit JSON lines instead of console text.
        log_file: Optional path; when given a file handler is added.
        stream: Optional stream for the console handler (defaults to stdout).

    Returns:
        The configured ``case_engine`` root logger.

    Raises:
        OSError: If *log_file* cannot be opened for writing (propagated
            unchanged — this is the caller's setup problem, not an
            engine error).

    Example:
        Idempotent reconfiguration (shown, not executed — installing a
        DEBUG JSON handler writes a ``case.log`` file and emits one
        DEBUG record describing the new handler set):

        >>> _ = configure_logging(level="DEBUG", json_mode=True, log_file="case.log")  # doctest: +SKIP

    """
    root = logging.getLogger(_ROOT_LOGGER)
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = JsonFormatter() if json_mode else ConsoleFormatter()
    console = logging.StreamHandler(stream or sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.propagate = False
    log.debug(
        "engine logging configured",
        extra={"level": str(level), "json_mode": json_mode,
               "log_file": log_file, "handlers": len(root.handlers)},
    )
    return root
