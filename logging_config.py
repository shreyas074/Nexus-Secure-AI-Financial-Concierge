"""Structured JSON logging + the @trace decorator.

Every log line is one JSON object. `@trace(logger)` writes:
  ENTER  (DEBUG) function name, call_id, bound arguments (redacted if redact= given)
  EXIT   (DEBUG) same call_id, duration_ms, truncated return preview
  FAILED (ERROR) same call_id, duration_ms, exception type, full traceback -> then re-raises
A request_id (contextvar) is added to every line so one request can be followed across layers.
"""
import contextvars
import functools
import inspect
import json
import logging
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

_request_id = contextvars.ContextVar("request_id", default="-")
PREVIEW_CHARS = 200


def set_request_id(value):
    return _request_id.set(value)


def get_request_id():
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record):
        line = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": os.getenv("SERVICE_NAME", "nexus"),
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        return json.dumps(line, default=str)


def configure_logging():
    """Attach JSON handlers to the 'nexus' logger once. LOG_LEVEL defaults to DEBUG."""
    root = logging.getLogger("nexus")
    if getattr(root, "_nexus_configured", False):
        return root
    root.setLevel(os.getenv("LOG_LEVEL", "DEBUG").upper())
    root.propagate = False
    handlers = []
    if os.getenv("NEXUS_LOG_STDERR", "1") != "0":
        handlers.append(logging.StreamHandler(sys.stderr))  # stderr keeps stdout clean
    log_file = os.getenv("NEXUS_LOG_FILE")
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers or [logging.NullHandler()]:
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root._nexus_configured = True
    return root


def get_logger(name):
    configure_logging()
    return logging.getLogger(f"nexus.{name}")


def _emit(logger, level, event, **fields):
    logger.log(level, event, extra={"fields": {"event": event, **fields}})


def _safe(redact, data):
    try:
        return redact(data) if redact else data
    except Exception:
        return {"redaction_error": True}


def trace(logger, redact=None):
    """Decorator factory. Works on plain functions and `async def` functions."""

    def decorator(fn):
        name = f"{fn.__module__}.{fn.__qualname__}"
        sig = inspect.signature(fn)

        def bound_args(args, kwargs):
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                data = {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
            except TypeError:
                data = {"args": list(args), **kwargs}
            return _safe(redact, data)

        def preview(result):
            if redact and isinstance(result, str):                   # a bare string may be customer text
                return f"<str len={len(result)}>"
            shown = _safe(redact, result) if redact else result      # key-wise redaction of dicts/lists
            text = repr(shown)
            if redact:                                               # string-level masking of what is left
                text = _safe(redact, {"result": text}).get("result", "<redacted>")
            return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "...<truncated>"

        def start(args, kwargs):
            call_id = uuid.uuid4().hex[:12]
            _emit(logger, logging.DEBUG, "ENTER", function=name, call_id=call_id,
                  args=bound_args(args, kwargs))
            return call_id, time.perf_counter()

        def ok(call_id, t0, result):
            _emit(logger, logging.DEBUG, "EXIT", function=name, call_id=call_id,
                  duration_ms=round((time.perf_counter() - t0) * 1000, 3), result_preview=preview(result))

        def failed(call_id, t0, exc):
            _emit(logger, logging.ERROR, "FAILED", function=name, call_id=call_id,
                  duration_ms=round((time.perf_counter() - t0) * 1000, 3),
                  exception_type=type(exc).__name__,
                  traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                call_id, t0 = start(args, kwargs)
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    failed(call_id, t0, exc)
                    raise
                ok(call_id, t0, result)
                return result

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            call_id, t0 = start(args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                failed(call_id, t0, exc)
                raise
            ok(call_id, t0, result)
            return result

        return wrapper

    return decorator
