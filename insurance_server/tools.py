"""insurance_server business logic: 3 tools + 1 resource. Touches ONLY policies + claims."""
import json
import re

from database import fetch_one
from errors import InvalidArgument, NotFound, resource_result, tool_result
from guardrails import mask_identifier, redact_for_logging, validate_id
from logging_config import get_logger, trace

SERVICE = "insurance_server"
logger = get_logger("insurance_server.tools")
_TAG = re.compile(r"^[a-z_]{3,40}$")


def _policy(policy_id):
    validate_id("policy", policy_id)
    row = fetch_one(SERVICE, "SELECT * FROM policies WHERE policy_id = ?", (policy_id,))
    if row is None:
        raise NotFound("Policy not found", code="POLICY_NOT_FOUND")
    return row


def _policy_view(row):
    return {
        "policy_ref": mask_identifier(row["policy_id"]),
        "policy_type": row["policy_type"],
        "premium_amount": row["premium_amount"],
        "premium_frequency": row["premium_frequency"],
        "payment_methods": row["payment_methods"].split(","),
        "status": row["status"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
    }


@tool_result
@trace(logger, redact=redact_for_logging)
def get_policy_details(policy_id: str) -> dict:
    """Single policy lookup: premiums, payment terms, status."""
    return _policy_view(_policy(policy_id))


@tool_result
@trace(logger, redact=redact_for_logging)
def check_coverage_clause(policy_id: str, scenario_tag: str) -> dict:
    """Deterministic lookup: is `scenario_tag` covered under this policy's clauses?
    covered is True / False, or None when the policy has no clause for that scenario."""
    if not isinstance(scenario_tag, str) or not _TAG.match(scenario_tag):
        raise InvalidArgument("scenario_tag must be lowercase letters/underscores, e.g. 'auto_debit_payment'")
    row = _policy(policy_id)
    clause = json.loads(row["coverage_clauses"]).get(scenario_tag)
    if clause is None:
        return {"policy_ref": mask_identifier(policy_id), "scenario_tag": scenario_tag,
                "covered": None, "clause": None, "clause_text": None}
    return {"policy_ref": mask_identifier(policy_id), "scenario_tag": scenario_tag,
            "covered": bool(clause["covered"]), "clause": clause["clause"], "clause_text": clause["text"]}


@tool_result
@trace(logger, redact=redact_for_logging)
def get_claim_status(claim_id: str) -> dict:
    """Claim STATUS only. This server has no settlement authority and returns no settlement amounts."""
    validate_id("claim", claim_id)
    row = fetch_one(SERVICE, "SELECT * FROM claims WHERE claim_id = ?", (claim_id,))
    if row is None:
        raise NotFound("Claim not found", code="CLAIM_NOT_FOUND")
    return {"claim_id": row["claim_id"], "policy_ref": mask_identifier(row["policy_id"]), "status": row["status"],
            "filed_on": row["filed_on"], "last_update": row["last_update"], "status_only": True}


@resource_result
@trace(logger, redact=redact_for_logging)
def policy_summary_resource(policy_id: str) -> dict:
    """policy://{policy_id}/summary - one policy, masked."""
    return _policy_view(_policy(policy_id))
