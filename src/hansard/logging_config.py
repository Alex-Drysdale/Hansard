"""Structured logging.

Phase 1 logged with printf-style strings, which is fine when a person is
watching a terminal and useless the moment a scheduler is running unattended:
"skipping debate X" cannot be counted, filtered or correlated.

Here every event is a name plus key/value pairs. The same call renders as
readable text on a terminal and as JSON in a container, so there is one logging
API and the destination decides the shape.

    log.warning("debate.skipped", ext_id=..., sitting_date=..., reason=...)

Log *facts*, not sentences: an event name that stays constant and fields that
vary. That is what makes "how many debates did we skip last week, and why"
answerable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# Third-party loggers that have nothing useful to say at INFO. httpx logs a line
# per request, which would bury our own events under thousands of near-identical
# ones; APScheduler narrates its own bookkeeping ("Adding job tentatively"),
# which duplicates the scheduler events we emit deliberately.
NOISY_LOGGERS = ("httpx", "httpcore", "yoyo", "apscheduler")


def configure(*, level: str = "INFO", log_format: str = "console") -> None:
    """Set up logging for the process. Safe to call more than once."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Route stdlib logging (ours and our dependencies') through structlog, so a
    # library's plain log line still comes out in the chosen format.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level, force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric_level, logging.WARNING))

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
        shared.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger bound to a module name."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_run(**values: Any) -> None:
    """Attach values to every log line emitted for the rest of this run.

    Used for the run id and job name, so a line from deep inside the HTTP client
    can still be traced back to the run that caused it without threading the
    identifier through every function signature.
    """
    structlog.contextvars.bind_contextvars(**values)


def clear_run() -> None:
    """Drop the current run's bound values."""
    structlog.contextvars.clear_contextvars()
