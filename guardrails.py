"""All safety controls in one module (spec section 4).

  validate_id              ID format checks (CUS/ACC/POL/PORT/PROD/CLM)
  mask_identifier          ACC-20077 -> ACC-****0077  (hard requirement on every tool/resource output)
  minimize_*_fields        caller_scope field filtering
  sanitize_free_text       length limit + HTML stripping + prompt-injection signatures
  check_output             output filter: unmasked IDs, unsupported numbers, non-compliant tone
  redact_for_logging       PII-safe log arguments (data minimisation)
"""
import re

from errors import InvalidArgument, InvalidIdFormat, PromptInjectionDetected
from logging_config import get_logger, trace

logger = get_logger("guardrails")

# ---------------------------------------------------------------- PII-safe logging
_ID_KEYS = {"account_id", "customer_id", "policy_id", "portfolio_id", "claim_id"}
_FREE_TEXT_KEYS = {"message", "customer_message", "customer_stated_intent", "text", "draft"}
_NAME_KEYS = {"name", "customer_name"}
LONG_TEXT = 60  # strings longer than this inside a log argument are treated as free text
_NAME_IN_TEXT = re.compile(r"""(["'](?:customer_)?name["']\s*:\s*["'])[^"']+""")
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")
_PHONE = re.compile(r"\+?\d[\d\s\-]{8,}\d")


def _mask_phone(value):
    digits = re.sub(r"\D", "", value)
    return "*" * max(len(digits) - 2, 0) + digits[-2:]


def mask_pii_in_text(text):
    """Mask ids, e-mails and phone numbers inside any string (used on log values)."""
    for kind in ID_FINDERS:
        text = ID_FINDERS[kind].sub(lambda m: mask_identifier(m.group(0)), text)
    text = _EMAIL.sub(lambda m: "***@" + m.group(1), text)
    text = _NAME_IN_TEXT.sub(r"\1<name>", text)
    return _PHONE.sub(lambda m: _mask_phone(m.group(0)), text)


def redact_for_logging(args):
    """Mask PII-shaped arguments before they reach a log line.

    Data minimisation: logs exist to debug behaviour, not to store customer data. So identifiers keep
    only their last four digits, e-mail keeps only the domain, phone keeps two digits, names and free text
    are reduced to a placeholder / their length, and any id/e-mail/phone/name hiding inside other strings
    is masked too.
    (Not @trace'd itself: the tracer calls it, so tracing it would recurse forever.)
    """
    if isinstance(args, dict):
        out = {}
        for key, value in args.items():
            if key in _ID_KEYS and isinstance(value, str):
                out[key] = mask_identifier(value)
            elif key == "phone" and isinstance(value, str):
                out[key] = _mask_phone(value)
            elif key == "email" and isinstance(value, str):
                out[key] = _EMAIL.sub(lambda m: "***@" + m.group(1), value)
            elif key in _FREE_TEXT_KEYS and isinstance(value, str):
                out[key] = f"<redacted text len={len(value)}>"
            elif key in _NAME_KEYS and isinstance(value, str):
                out[key] = "<name>"
            elif isinstance(value, str) and len(value) > LONG_TEXT and key not in ("sql", "traceback"):
                out[key] = f"<redacted text len={len(value)}>"  # long free-form text: log its size only
            else:
                out[key] = redact_for_logging(value)
        return out
    if isinstance(args, (list, tuple)):
        return [redact_for_logging(v) for v in args]
    if isinstance(args, str):
        return mask_pii_in_text(args)
    return args


# ---------------------------------------------------------------- ID validation
ID_PATTERNS = {
    "customer": re.compile(r"^CUS-\d{5}$"),
    "account": re.compile(r"^ACC-\d{5}$"),
    "policy": re.compile(r"^POL-[A-Z]{2}-\d{5}$"),
    "portfolio": re.compile(r"^PORT-\d{5}$"),
    "product": re.compile(r"^PROD-[A-Z]{2}-\d{2}$"),
    "claim": re.compile(r"^CLM-\d{5}$"),
}
# Same shapes, unanchored, for finding IDs inside free text
ID_FINDERS = {
    "account": re.compile(r"\bACC-\d{5}\b"),
    "policy": re.compile(r"\bPOL-[A-Z]{2}-\d{5}\b"),
    "portfolio": re.compile(r"\bPORT-\d{5}\b"),
    "customer": re.compile(r"\bCUS-\d{5}\b"),
    "claim": re.compile(r"\bCLM-\d{5}\b"),
    "product": re.compile(r"\bPROD-[A-Z]{2}-\d{2}\b"),
}
# The identifiers that must never leave the system unmasked
NUMBER_KINDS = ("account", "policy", "portfolio")


@trace(logger, redact=redact_for_logging)
def validate_id(kind, value):
    """Return value if it matches the format for `kind`, else raise a typed InvalidIdFormat."""
    pattern = ID_PATTERNS.get(kind)
    if pattern is None:
        raise InvalidArgument(f"Unknown id kind '{kind}'")
    if not isinstance(value, str) or not pattern.match(value):
        raise InvalidIdFormat(f"Invalid {kind} id format", kind=kind)
    return value


# ---------------------------------------------------------------- masking
def mask_identifier(value):
    """'ACC-20077' -> 'ACC-****0077'. Keeps only the last four digits. Idempotent."""
    if not isinstance(value, str) or "****" in value:
        return value
    match = re.fullmatch(r"(.*-)(\d+)", value)
    if match:
        return f"{match.group(1)}****{match.group(2)[-4:]}"
    return "****" + value[-4:]


@trace(logger, redact=redact_for_logging)
def mask_numbers_in_text(text):
    """Mask every account / policy / portfolio number found inside a piece of text."""
    for kind in NUMBER_KINDS:
        text = ID_FINDERS[kind].sub(lambda m: mask_identifier(m.group(0)), text)
    return text


@trace(logger, redact=redact_for_logging)
def find_unmasked_numbers(text):
    return [m.group(0) for kind in NUMBER_KINDS for m in ID_FINDERS[kind].finditer(text)]


# ---------------------------------------------------------------- scoped field visibility
SCOPES = ("customer_self", "agent_internal")


def _check_scope(caller_scope):
    if caller_scope not in SCOPES:
        raise InvalidArgument(f"caller_scope must be one of {SCOPES}", code="INVALID_SCOPE")


@trace(logger, redact=redact_for_logging)
def minimize_account_fields(row, caller_scope):
    """Return only the account fields this scope may see (ids always masked)."""
    _check_scope(caller_scope)
    out = {
        "account_ref": mask_identifier(row["account_id"]),
        "account_type": row["account_type"],
        "status": row["status"],
        "balance": row["balance"],
        "currency": row["currency"],
        "auto_debit_allowed": bool(row["auto_debit_allowed"]),
    }
    if caller_scope == "agent_internal":
        out["customer_id"] = row["customer_id"]
        out["opened_on"] = row["opened_on"]
    return out


@trace(logger, redact=redact_for_logging)
def minimize_portfolio_fields(row, caller_scope, holdings=None):
    """Portfolio equivalent of minimize_account_fields. Holdings only for agent_internal."""
    _check_scope(caller_scope)
    out = {
        "portfolio_ref": mask_identifier(row["portfolio_id"]),
        "risk_profile": row["risk_profile"],
        "total_value": row["total_value"],
        "currency": row["currency"],
        "status": row["status"],
    }
    if caller_scope == "agent_internal":
        out["customer_id"] = row["customer_id"]
        out["holdings"] = holdings or []
    return out


# ---------------------------------------------------------------- sanitisation / injection defence
MAX_TEXT_LEN = 1000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_INJECTION_SIGNATURES = {
    "ignore_previous": r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|preceding)\s+(instructions|prompts?|rules|messages)",
    "disregard": r"disregard\s+(all\s+|any\s+|the\s+|your\s+)?(previous\s+|prior\s+|above\s+|earlier\s+)?(instructions|rules|guidelines|policies)",
    "forget": r"forget\s+(everything|all\s+previous|your\s+instructions)",
    "role_override": r"you\s+are\s+now\s+(a|an|the)\b|from\s+now\s+on,?\s+you|act\s+as\s+(an?\s+)?(admin|administrator|system|developer|root|unrestricted)",
    "prompt_leak": r"(reveal|show|print|repeat|output)\s+(me\s+)?(your\s+|the\s+)?(system|hidden|initial)\s+(prompt|instructions)",
    "data_exfil": r"(reveal|show|send|output|display|print)\s+(me\s+)?(the\s+)?(full|complete|unmasked|raw)\s+(account|policy|portfolio|card|kyc)",
    "special_tokens": r"<\|.*?\|>|\[/?INST\]|###\s*(system|instruction)",
    "jailbreak": r"developer\s+mode|jailbreak|do\s+anything\s+now",
    "override_safety": r"override\s+(your|the|all)\s+(safety|security|rules|instructions|guardrails)",
    "new_instructions": r"new\s+instructions\s*:",
}
_INJECTION_RE = {name: re.compile(rx, re.I | re.S) for name, rx in _INJECTION_SIGNATURES.items()}


def _scan_for_injection(text):
    for name, rx in _INJECTION_RE.items():
        if rx.search(text):
            raise PromptInjectionDetected("Input contains a prompt-injection signature", signature=name)


@trace(logger, redact=redact_for_logging)
def sanitize_free_text(text, max_len=MAX_TEXT_LEN):
    """Clean untrusted text: scan -> strip HTML -> scan again -> length limit.

    Scanning twice matters: the first scan sees hidden HTML comments, the second sees signatures
    that were split by tags (ig<b>nore ...). Raises PromptInjectionDetected on a match.
    """
    if not isinstance(text, str):
        raise InvalidArgument("Free text must be a string")
    _scan_for_injection(text)
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    cleaned = re.sub(r"<(script|style)\b.*?</\1>", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    _scan_for_injection(cleaned)
    return cleaned[:max_len]


# ---------------------------------------------------------------- output filter
_BANNED_PHRASES = ["guarantee", "guaranteed", "risk-free", "no risk", "definitely will", "you must", "cannot lose"]
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text):
    """Number tokens, canonicalised so 184500.0 and 184500 compare equal."""
    found = _NUMBER.findall(text.replace(",", ""))
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in found}


@trace(logger, redact=redact_for_logging)
def check_output(text, context_blob):
    """Validate a draft before it reaches the customer. Returns a list of violations (empty = clean).

    - unmasked_identifier : a full ACC-/POL-/PORT- number is present
    - unsupported_number  : a number that does not appear anywhere in the grounded context
    - non_compliant_tone  : promises / guarantees that a regulated firm must not make
    """
    violations = [{"type": "unmasked_identifier", "value": mask_identifier(v)} for v in find_unmasked_numbers(text)]
    for number in sorted(_numbers(text) - _numbers(context_blob)):
        violations.append({"type": "unsupported_number", "value": number})
    lowered = text.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in lowered:
            violations.append({"type": "non_compliant_tone", "value": phrase})
    return violations
