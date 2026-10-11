from mcp.server.fastmcp import FastMCP

import config
from insurance_server import tools

NAME = "insurance_server"


def build_server(host=config.HOST, port=config.PORTS[NAME]):
    mcp = FastMCP(NAME, host=host, port=port)
    mcp.tool()(tools.get_policy_details)
    mcp.tool()(tools.check_coverage_clause)
    mcp.tool()(tools.get_claim_status)
    mcp.resource("policy://{policy_id}/summary")(tools.policy_summary_resource)
    return mcp
