"""Deterministic classification rules - pure functions, no LLM, easy to unit-test.

The escalation lexicon is the heart of Stress Test 3: it is regex-based, covers many phrasings, and the
LLM (if enabled) may only RAISE the category, never lower it (see merge_escalation).
"""
import re

from guardrails import redact_for_logging
from logging_config import get_logger, trace

logger = get_logger("rules")

# Highest severity first. Used for both detection order and the 'raise-only' merge.
SEVERITY = ["none", "compliance_override_request", "financial_hardship", "safeguarding_concern"]

_LEXICON = {
    "safeguarding_concern": [
        r"being forced", r"forcing me", r"pressur(e|ed|ing) me", r"controlling my", r"coerc", r"\bscam", r"elderly",
        r"threaten", r"vulnerable", r"\babus", r"taking my money", r"won'?t let me (access|see|use)", r"afraid",
    ],
    "financial_hardship": [
        r"lost my job", r"laid off", r"made redundant", r"redundan", r"can'?t (keep up|afford|pay|make ends)",
        r"cannot (afford|pay|keep up)", r"unable to (pay|afford|make)", r"struggling (to|with)",
        r"behind on", r"missed (a |my |several )?payments?", r"(money|finances|things)('s| is| are| has| have)? ?(been )?tight",
        r"\bin debt\b", r"\bdebts?\b", r"hardship", r"can'?t cope", r"no income", r"outgoings", r"pause my premium",
        r"payment (holiday|break)",
    ],
    "compliance_override_request": [
        r"skip (the |your )?(kyc|verification|checks?|id)", r"bypass", r"override (the |your )?(limit|checks?|policy|verification)",
        r"without (any )?(verification|kyc|checks?|id)", r"waive (the )?(verification|kyc|checks?)",
        r"make an exception", r"off the record", r"don'?t (log|record) (this|it)",
    ],
}
_LEXICON_RE = {cat: [re.compile(p, re.I) for p in pats] for cat, pats in _LEXICON.items()}


@trace(logger, redact=redact_for_logging)
def detect_escalation_category(text):
    """Most severe category whose lexicon matches, else 'none'."""
    for category in reversed(SEVERITY[1:]):  # safeguarding first
        if any(rx.search(text) for rx in _LEXICON_RE[category]):
            return category
    return "none"


@trace(logger, redact=redact_for_logging)
def merge_escalation(rule_category, llm_category):
    """The LLM may raise the category but never lower it."""
    return max(rule_category, llm_category or "none", key=lambda c: SEVERITY.index(c) if c in SEVERITY else 0)


_PRODUCT_WORDS = {
    "banking": r"account|savings|balance|transaction|transfer|deposit|debit|card|statement",
    "insurance": r"insurance|policy|premium|claim|cover|covered|coverage|insured",
    "wealth": r"portfolio|invest|fund|mutual|equity|sip\b|risk profile|holdings",
}
_REQUEST_TYPES = [  # first match wins
    ("auto_debit_setup", r"auto[- ]?debit|direct debit|standing instruction"),
    ("claim_status", r"\bclaim"),
    ("balance_inquiry", r"balance|how much (money )?do i have"),
    ("coverage_question", r"covered|coverage|cover\b|payment terms"),
    ("portfolio_query", r"portfolio|invest|fund|holdings"),
    ("complaint", r"complain|unhappy|terrible|unacceptable|disappointed"),
]
_CRITICAL = r"urgent|immediately|fraud|stolen|unauthori[sz]ed"
_HIGH = r"asap|soon|today|as soon as"


@trace(logger, redact=redact_for_logging)
def classify_rules(text):
    """Return the model-free classification of a (sanitised) message."""
    lowered = text.lower()
    lines = [line for line, pat in _PRODUCT_WORDS.items() if re.search(pat, lowered)]
    product_line = lines[0] if len(lines) == 1 else "cross_product" if len(lines) > 1 else "banking"
    request_type = next((t for t, pat in _REQUEST_TYPES if re.search(pat, lowered)), "other")
    urgency = "critical" if re.search(_CRITICAL, lowered) else "high" if re.search(_HIGH, lowered) else "routine"
    return {"product_line": product_line, "request_type": request_type, "urgency": urgency,
            "escalation_category": detect_escalation_category(text)}


_ID_PATTERNS = {
    "account_id": r"\bACC-\d{5}\b", "policy_id": r"\bPOL-[A-Z]{2}-\d{5}\b", "portfolio_id": r"\bPORT-\d{5}\b",
    "claim_id": r"\bCLM-\d{5}\b", "product_id": r"\bPROD-[A-Z]{2}-\d{2}\b",
}


@trace(logger, redact=redact_for_logging)
def extract_ids(text):
    """Product ids the customer typed into the message."""
    found = {}
    for key, pattern in _ID_PATTERNS.items():
        match = re.search(pattern, text)
        if match:
            found[key] = match.group(0)
    return found


_SCENARIO_TAGS = [("auto_debit_payment", r"auto[- ]?debit"), ("theft", r"theft|stolen|burglar"),
                  ("flood_damage", r"flood|water damage")]


@trace(logger, redact=redact_for_logging)
def infer_scenario_tag(text, request_type):
    for tag, pattern in _SCENARIO_TAGS:
        if re.search(pattern, text, re.I):
            return tag
    return "auto_debit_payment" if request_type == "auto_debit_setup" else None
