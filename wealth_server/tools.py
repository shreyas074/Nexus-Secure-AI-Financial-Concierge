"""wealth_server business logic: 3 tools. Touches ONLY portfolios + investment_products."""
import json

from database import fetch_one
from errors import InvalidArgument, NotFound, tool_result
from guardrails import minimize_portfolio_fields, redact_for_logging, validate_id
from logging_config import get_logger, trace

SERVICE = "wealth_server"
logger = get_logger("wealth_server.tools")

# highest product risk level (1-5) each risk profile may hold
MAX_RISK_LEVEL = {"conservative": 2, "moderate": 3, "aggressive": 5}


@tool_result
@trace(logger, redact=redact_for_logging)
def get_portfolio_summary(portfolio_id: str, caller_scope: str) -> dict:
    """Portfolio lookup. Holdings are only visible to caller_scope='agent_internal'."""
    validate_id("portfolio", portfolio_id)
    row = fetch_one(SERVICE, "SELECT * FROM portfolios WHERE portfolio_id = ?", (portfolio_id,))
    if row is None:
        raise NotFound("Portfolio not found", code="PORTFOLIO_NOT_FOUND")
    return minimize_portfolio_fields(row, caller_scope, json.loads(row["holdings_json"]))


@tool_result
@trace(logger, redact=redact_for_logging)
def get_investment_product_details(product_id: str) -> dict:
    """Single investment product lookup."""
    validate_id("product", product_id)
    row = fetch_one(SERVICE, "SELECT * FROM investment_products WHERE product_id = ?", (product_id,))
    if row is None:
        raise NotFound("Product not found", code="PRODUCT_NOT_FOUND")
    return dict(row)


@tool_result
@trace(logger, redact=redact_for_logging)
def check_product_suitability(product_id: str, risk_profile: str) -> dict:
    """Is the product's risk level within the limit for this risk profile? Read-only; no customer data."""
    validate_id("product", product_id)
    if risk_profile not in MAX_RISK_LEVEL:
        raise InvalidArgument(f"risk_profile must be one of {sorted(MAX_RISK_LEVEL)}")
    row = fetch_one(SERVICE, "SELECT product_id, name, risk_level FROM investment_products WHERE product_id = ?",
                    (product_id,))
    if row is None:
        raise NotFound("Product not found", code="PRODUCT_NOT_FOUND")
    limit = MAX_RISK_LEVEL[risk_profile]
    suitable = row["risk_level"] <= limit
    return {"product_id": product_id, "product_name": row["name"], "risk_profile": risk_profile,
            "product_risk_level": row["risk_level"], "max_allowed_level": limit, "suitable": suitable,
            "reason": f"Product risk level {row['risk_level']} is {'within' if suitable else 'above'} "
                      f"the limit of {limit} for a {risk_profile} profile."}
