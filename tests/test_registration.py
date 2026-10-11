"""Registration checks against the REAL `mcp` package (skipped, with a reason, if only a stub is installed)."""
import asyncio

import pytest

from banking_server.server import build_server as banking
from concierge_ops_server.server import build_server as ops
from insurance_server.server import build_server as insurance
from nexus_orchestrator.server import build_server as orchestrator
from wealth_server.server import build_server as wealth

try:
    from mcp.server.fastmcp import FastMCP
    REAL_MCP = hasattr(FastMCP, "list_tools")
except ImportError:
    REAL_MCP = False

EXPECTED = {  # server -> (tools, resource templates, prompts)
    "banking": (banking, {"get_account_summary", "get_transaction_history", "get_linked_accounts"},
                {"account://{account_id}/summary"}, set()),
    "insurance": (insurance, {"get_policy_details", "check_coverage_clause", "get_claim_status"},
                  {"policy://{policy_id}/summary"}, set()),
    "wealth": (wealth, {"get_portfolio_summary", "get_investment_product_details", "check_product_suitability"},
               set(), set()),
    "ops": (ops, {"get_customer_kyc_status", "run_escalation_check", "send_customer_notification", "write_audit_log"},
            {"customer://{customer_id}/profile", "knowledgebase://{document_id}/excerpt"},
            {"request_classification_prompt", "response_drafting_prompt"}),
}


def listing(server):
    async def go():
        return ({t.name for t in await server.list_tools()},
                {str(r.uriTemplate) for r in await server.list_resource_templates()},
                {p.name for p in await server.list_prompts()})
    return asyncio.run(go())


@pytest.mark.skipif(not REAL_MCP, reason="real `mcp` package not installed - run `pip install -r requirements.txt`")
def test_every_server_registers_exactly_its_primitives_with_real_fastmcp():
    all_tools, all_resources, all_prompts = set(), set(), set()
    for name, (build, tools, resources, prompts) in EXPECTED.items():
        got = listing(build())
        assert got == (tools, resources, prompts), name
        all_tools |= got[0]; all_resources |= got[1]; all_prompts |= got[2]
    assert len(all_tools) == 13 and len(all_resources) == 4 and len(all_prompts) == 2


@pytest.mark.skipif(not REAL_MCP, reason="real `mcp` package not installed")
def test_orchestrator_server_exposes_handle_customer_request():
    tools, _, _ = listing(orchestrator())
    assert tools == {"handle_customer_request"}


@pytest.mark.skipif(not REAL_MCP, reason="real `mcp` package not installed")
def test_tool_schemas_expose_caller_scope_argument():
    async def go():
        return {t.name: t.inputSchema for t in await banking().list_tools()}
    schemas = asyncio.run(go())
    assert "caller_scope" in schemas["get_account_summary"]["properties"]
