"""Agent 4 - Validation Agent: fact-check the draft against the grounded context and score confidence.

Confidence is a formula you can write on a whiteboard:
    base        = supported claims / total claims        (0 if the draft has no claims at all)
    - 0.30      if a retrieval conflict exists and the draft does not acknowledge it
    - 0.50      if the output filter finds a violation (unmasked id, unsupported number, banned promise)
Conflicts are SURFACED here (returned in `conflicts_surfaced`), never resolved silently.
"""
import json

from guardrails import check_output, redact_for_logging
from logging_config import get_logger, trace
from nexus_orchestrator import llm

logger = get_logger("agent.validator")


@trace(logger, redact=redact_for_logging)
def claim_supported(claim, ctx):
    if claim["source"] == "fact":
        return claim["key"] in ctx["facts"] and ctx["facts"][claim["key"]] == claim["value"]
    if claim["source"] == "doc":
        return claim["doc_id"] in {c["doc_id"] for c in ctx["chunks"]}
    if claim["source"] == "conflict":
        return claim["topic"] in {c["topic"] for c in ctx["conflicts"]}
    return False


class Validator:
    @trace(logger, redact=redact_for_logging)
    def run(self, intake, ctx, draft):
        claims = draft["claims"]
        supported = [c for c in claims if claim_supported(c, ctx)]
        base = len(supported) / len(claims) if claims else 0.0
        acknowledged = all(any(cl["source"] == "conflict" and cl["topic"] == c["topic"] for cl in claims)
                           for c in ctx["conflicts"])
        blob = json.dumps(ctx["facts"], default=str) + " " + " ".join(c["text"] for c in ctx["chunks"])
        violations = check_output(draft["text"], blob)
        confidence = base - (0.0 if acknowledged else 0.3) - (0.5 if violations else 0.0)
        issues = []
        if not claims:
            issues.append("draft has no verifiable claims")
        if len(supported) < len(claims):
            issues.append(f"{len(claims) - len(supported)} claim(s) not supported by the grounded context")
        if not acknowledged:
            issues.append("a retrieval conflict was not acknowledged in the draft")
        issues += [f"{v['type']}: {v['value']}" for v in violations]
        if llm.llm_enabled():
            review = llm.parse_json(llm.run_agent(
                role="Validation Agent", goal="Fact-check a draft against the context and rate your confidence",
                backstory="You are a strict compliance reviewer for a regulated financial firm.",
                prompt=("Context:\n" + ctx["summary"] + "\n\nDraft:\n" + draft["text"] +
                        '\n\nReturn ONLY JSON: {"confidence": 0-1, "issues": ["..."]}'),
                expected_output="A single JSON object"))
            if isinstance(review.get("confidence"), (int, float)):
                confidence = min(confidence, float(review["confidence"]))  # the LLM can only lower confidence
            issues += [str(i) for i in review.get("issues", [])][:5]
        return {"confidence": round(max(0.0, min(1.0, confidence)), 2), "claims_supported": len(supported),
                "claims_total": len(claims), "conflicts_surfaced": ctx["conflicts"],
                "conflicts_acknowledged": acknowledged, "violations": violations, "issues": issues,
                "approved": not violations}
