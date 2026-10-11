"""Agent 5 - Escalation Agent: ask the deterministic gate whether a human must take over.

The decision ALWAYS comes from the `run_escalation_check` MCP tool (hard categories first, then flags on the
customer's record, then the confidence threshold). In Claude mode a CrewAI agent may also call the tool and
write a routing note, but that note is advisory only: the pipeline reads the tool's answer, never the model's.
"""
from guardrails import redact_for_logging
from knowledge_base import find_document
from logging_config import get_logger, trace
from nexus_orchestrator import llm

logger = get_logger("agent.escalation")
OPS = "concierge_ops_server"


TEAM_NAMES = {"hardship_support_team": "hardship support team", "safeguarding_team": "safeguarding team",
              "compliance_officer": "compliance team", "general_support_agent": "customer support team"}


@trace(logger, redact=redact_for_logging)
def acknowledgement(name, route):
    """Escalation notice built from the communication template (no model-generated promises)."""
    _, body = find_document("TEMPLATE_ESCALATION_ACK")
    template = body.split("\n", 1)[1]  # drop the '## Acknowledgement' heading line
    return template.replace("{name}", name).replace("{team}", TEAM_NAMES.get(route, "customer support team"))


class EscalationAgent:
    def __init__(self, gateway):
        self.gateway = gateway

    @trace(logger, redact=redact_for_logging)
    def run(self, intake, confidence, customer_name):
        decision = self.gateway.call(OPS, "run_escalation_check", customer_id=intake.customer_id,
                                     request_category=intake.escalation_category.value, draft_confidence=confidence)
        if llm.llm_enabled():
            decision["advisory_note"] = llm.run_agent(
                role="Escalation Agent", goal="Explain where this request should be routed",
                backstory="You always call run_escalation_check first and never override it.",
                prompt=(f"Call run_escalation_check for customer {intake.customer_id}, category "
                        f"{intake.escalation_category.value}, confidence {confidence}. Then write one sentence of routing advice."),
                expected_output="One sentence", tools=[llm.make_escalation_tool(self.gateway)])
        if decision["escalate"]:
            decision["acknowledgement"] = acknowledgement(customer_name, decision.get("route"))
        return decision
