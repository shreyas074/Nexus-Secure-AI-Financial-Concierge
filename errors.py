"""Typed errors shared by every layer.

Every error has a stable `code`, so callers (and tests) branch on the code,
never on message text. MCP tools never leak Python exceptions to the client:
`tool_result` turns them into {"ok": False, "error": {...}} dictionaries.
"""
import functools
import json


class NexusError(Exception):
    code = "NEXUS_ERROR"

    def __init__(self, message="", code=None, **details):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class InvalidIdFormat(NexusError):
    code = "INVALID_ID_FORMAT"


class InvalidArgument(NexusError):
    code = "INVALID_ARGUMENT"


class NotFound(NexusError):
    code = "NOT_FOUND"


class PromptInjectionDetected(NexusError):
    code = "PROMPT_INJECTION_DETECTED"


class IntakeValidationError(NexusError):
    code = "INTAKE_VALIDATION_FAILED"


class CustomerNotFound(NexusError):
    code = "CUSTOMER_NOT_FOUND"


class ServerUnavailableError(NexusError):
    code = "SERVER_UNAVAILABLE"


class NexusToolError(NexusError):
    """A backend MCP tool answered with ok=False. `code` is copied from the server."""


class LLMUnavailable(NexusError):
    code = "LLM_UNAVAILABLE"


def tool_result(fn):
    """Wrap a tool so it ALWAYS returns {"ok": True, "data": ...} or {"ok": False, "error": ...}."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": fn(*args, **kwargs)}
        except NexusError as exc:
            return {"ok": False, "error": exc.to_dict()}
        except Exception:  # never leak internals; the traceback is already in the FAILED log line
            return {"ok": False, "error": {"code": "INTERNAL_ERROR",
                                            "message": "Unexpected server error (see server log)",
                                            "details": {}}}

    return wrapper


def resource_result(fn):
    """Same as tool_result, but resources must return text, so the dict is JSON-encoded."""
    inner = tool_result(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return json.dumps(inner(*args, **kwargs), default=str)

    return wrapper
