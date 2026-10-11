from mcp.server.fastmcp import FastMCP

import config
from concierge_ops_server import tools

NAME = "concierge_ops_server"


def build_server(host=config.HOST, port=config.PORTS[NAME]):
    mcp = FastMCP(NAME, host=host, port=port)
    mcp.tool()(tools.get_customer_kyc_status)
    mcp.tool()(tools.run_escalation_check)
    mcp.tool()(tools.send_customer_notification)
    mcp.tool()(tools.write_audit_log)
    mcp.resource("customer://{customer_id}/profile")(tools.customer_profile_resource)
    mcp.resource("knowledgebase://{document_id}/excerpt")(tools.knowledge_excerpt_resource)
    mcp.prompt()(tools.request_classification_prompt)
    mcp.prompt()(tools.response_drafting_prompt)
    return mcp
