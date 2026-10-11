"""banking_server business logic: 3 tools + 1 resource. Touches ONLY accounts + transactions."""
from database import fetch_all, fetch_one
from errors import InvalidArgument, NotFound, resource_result, tool_result
from guardrails import (mask_identifier, minimize_account_fields, redact_for_logging,
                        validate_id)
from logging_config import get_logger, trace

SERVICE = "banking_server"
logger = get_logger("banking_server.tools")


@tool_result
@trace(logger, redact=redact_for_logging)
def get_account_summary(account_id: str, caller_scope: str) -> dict:
    """Account lookup. Fields are minimised per caller_scope (customer_self | agent_internal)."""
    validate_id("account", account_id)
    row = fetch_one(SERVICE, "SELECT * FROM accounts WHERE account_id = ?", (account_id,))
    if row is None:
        raise NotFound("Account not found", code="ACCOUNT_NOT_FOUND")
    return minimize_account_fields(row, caller_scope)


@tool_result
@trace(logger, redact=redact_for_logging)
def get_transaction_history(account_id: str, limit: int = 5) -> dict:
    """Recent transactions, most recent first (limit 1-50)."""
    validate_id("account", account_id)
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise InvalidArgument("limit must be an integer between 1 and 50")
    rows = fetch_all(SERVICE, "SELECT txn_date, description, amount, balance_after FROM transactions "
                              "WHERE account_id = ? ORDER BY txn_date DESC, txn_id DESC LIMIT ?",
                     (account_id, limit))
    return {"account_ref": mask_identifier(account_id), "count": len(rows), "transactions": rows}


@tool_result
@trace(logger, redact=redact_for_logging)
def get_linked_accounts(customer_id: str, caller_scope: str = "customer_self") -> dict:
    """All accounts linked to a customer, with the same field minimisation."""
    validate_id("customer", customer_id)
    rows = fetch_all(SERVICE, "SELECT * FROM accounts WHERE customer_id = ? ORDER BY account_id", (customer_id,))
    return {"count": len(rows), "accounts": [minimize_account_fields(r, caller_scope) for r in rows]}


@resource_result
@trace(logger, redact=redact_for_logging)
def account_summary_resource(account_id: str) -> dict:
    """account://{account_id}/summary - one account, customer_self fields, masked."""
    validate_id("account", account_id)
    row = fetch_one(SERVICE, "SELECT * FROM accounts WHERE account_id = ?", (account_id,))
    if row is None:
        raise NotFound("Account not found", code="ACCOUNT_NOT_FOUND")
    return minimize_account_fields(row, "customer_self")
