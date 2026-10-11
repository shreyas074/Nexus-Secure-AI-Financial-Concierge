"""The orchestrator exposed as its own MCP server (port 8005): one tool, `handle_customer_request`.
That makes the orchestrator a real standalone process that answers a live MCP client, while it is itself
an MCP *client* of the four backend servers."""
import asyncio

from mcp.server.fastmcp import FastMCP

import config
from guardrails import redact_for_logging
from logging_config import get_logger, trace
from nexus_orchestrator.gateway import Gateway
from nexus_orchestrator.pipeline import NexusPipeline

NAME = "nexus_orchestrator"
logger = get_logger("orchestrator.server")


def build_server(host=config.HOST, port=config.PORTS[NAME], gateway_mode="http"):
    mcp = FastMCP(NAME, host=host, port=port)
    holder = {}

    @mcp.tool()
    @trace(logger, redact=redact_for_logging)
    async def handle_customer_request(customer_id: str, message: str, account_id: str = "", policy_id: str = "",
                                      portfolio_id: str = "", claim_id: str = "", product_id: str = "") -> dict:
        """Run one customer message through intake -> retrieval -> agents -> MCP backends."""
        if "pipeline" not in holder:
            holder["pipeline"] = NexusPipeline(Gateway(mode=gateway_mode))
        context = {"account_id": account_id, "policy_id": policy_id, "portfolio_id": portfolio_id,
                   "claim_id": claim_id, "product_id": product_id}
        # The pipeline is synchronous (and uses asyncio.run inside the gateway), so run it in a worker thread.
        return await asyncio.to_thread(holder["pipeline"].handle, message, customer_id, context)

    return mcp
