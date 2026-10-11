"""The orchestrator: one request flows classify -> retrieve -> draft -> validate -> escalate, then acts.

Read `handle()` top to bottom - it is the whole system on one screen. Safety gates are plain Python
between the agents, so no model output can skip them.
"""
import json

from errors import NexusError, NexusToolError
from guardrails import redact_for_logging
from knowledge_base import KnowledgeIndex
from logging_config import get_logger, set_request_id, trace
from nexus_orchestrator.agents import Classifier, Drafter, EscalationAgent, Retriever, Validator
from nexus_orchestrator.gateway import Gateway
from nexus_orchestrator.schemas import new_request_id

logger = get_logger("pipeline")
OPS = "concierge_ops_server"


class NexusPipeline:
    def __init__(self, gateway=None, index=None):
        self.gateway = gateway or Gateway()
        self.index = index or KnowledgeIndex()
        self.classifier = Classifier(self.gateway)
        self.retriever = Retriever(self.gateway, self.index)
        self.drafter = Drafter(self.gateway)
        self.validator = Validator()
        self.escalator = EscalationAgent(self.gateway)

    # ----------------------------------------------------------------- public entry point
    @trace(logger, redact=redact_for_logging)
    def handle(self, message, customer_id, context=None):
        request_id = new_request_id()
        set_request_id(request_id)
        result = {"request_id": request_id, "status": None, "steps": []}

        def step(agent, summary):
            result["steps"].append({"agent": agent, "summary": summary})

        try:
            # 1. CLASSIFIER - validate into the typed schema. Failure stops everything (no tool calls yet).
            intake = self.classifier.run(message, customer_id, request_id, context)
            result["intake"] = json.loads(intake.model_dump_json())
            step("classifier", f"{intake.product_line.value}/{intake.request_type.value}, "
                               f"urgency={intake.urgency.value}, escalation_category={intake.escalation_category.value}")

            # 1b. Identity gate - still part of intake: unknown customer = typed error, nothing else runs.
            self.gateway.call(OPS, "get_customer_kyc_status", customer_id=intake.customer_id)
            profile = self.gateway.read_resource(OPS, f"customer://{intake.customer_id}/profile")
            name = profile["name"]

            # 2. RETRIEVER - live MCP data + knowledge base, reconciled; injections quarantined.
            ctx = self.retriever.run(intake)
            result["context"] = {k: ctx[k] for k in ("facts", "conflicts", "agreements", "quarantined",
                                                     "missing_context", "sources")}
            result["context"]["chunk_ids"] = [c["chunk_id"] for c in ctx["chunks"]]
            step("retriever", f"{len(ctx['facts'])} facts, {len(ctx['chunks'])} chunks, "
                              f"{len(ctx['conflicts'])} conflict(s), {len(ctx['quarantined'])} quarantined")

            # 3. DRAFTING AGENT - grounded context only.
            draft = self.drafter.run(intake, ctx, name)
            step("drafter", f"{len(draft['claims'])} claim(s) drafted")

            # 4. VALIDATION AGENT - fact-check, surface conflicts, score confidence.
            validation = self.validator.run(intake, ctx, draft)
            result["validation"] = validation
            result["confidence"] = validation["confidence"]
            step("validator", f"confidence={validation['confidence']}, issues={len(validation['issues'])}")

            # 5. ESCALATION AGENT - the decision is the MCP tool's, never the model's.
            decision = self.escalator.run(intake, validation["confidence"], name)
            result["escalation"] = decision
            step("escalation", f"escalate={decision['escalate']} reason={decision['reason']}")

            # 6. Act: notify the customer and audit the outcome (both are MCP writes).
            if decision["escalate"]:
                result["status"] = "escalated"
                result["draft_for_specialist"] = draft["text"]
                result["response"] = decision["acknowledgement"]
            else:
                result["status"] = "resolved"
                result["response"] = draft["text"]
            result["notification"] = self._notify(intake.customer_id, result["response"])
            self._audit(f"request_{result['status']}", intake.customer_id,
                        f"{result['request_id']} reason={decision['reason']} confidence={validation['confidence']}")
        except NexusError as exc:
            # Typed failure. ServerUnavailableError lands here too: a clear error, never a guessed answer.
            result.update(status="rejected" if exc.code in REJECTION_CODES else "error", error=exc.to_dict())
        except Exception as exc:  # last resort: never crash the caller
            logger.exception("unexpected pipeline failure")
            result.update(status="error", error={"code": "INTERNAL_ERROR", "message": type(exc).__name__, "details": {}})
        return result

    # ----------------------------------------------------------------- helpers
    def _notify(self, customer_id, message):
        try:
            return self.gateway.call(OPS, "send_customer_notification", customer_id=customer_id,
                                     channel="in_app", message=message)
        except NexusToolError as exc:  # e.g. KYC_NOT_VERIFIED: report it, do not hide it
            return {"sent": False, "error": exc.to_dict()}

    def _audit(self, action, customer_id, outcome):
        return self.gateway.call(OPS, "write_audit_log", action_type=action, performed_by="nexus_orchestrator",
                                 customer_id=customer_id, outcome=outcome)


# The request itself is bad (caller's fault) -> "rejected". Anything else (server down, bug) -> "error".
REJECTION_CODES = {"INTAKE_VALIDATION_FAILED", "PROMPT_INJECTION_DETECTED", "INVALID_ID_FORMAT", "ACCOUNT_NOT_LINKED",
                   "INVALID_ARGUMENT", "CUSTOMER_NOT_FOUND", "ACCOUNT_NOT_FOUND", "POLICY_NOT_FOUND",
                   "CLAIM_NOT_FOUND", "PORTFOLIO_NOT_FOUND", "PRODUCT_NOT_FOUND"}
