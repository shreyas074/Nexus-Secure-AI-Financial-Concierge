from mcp.server.fastmcp import FastMCP

import config
from banking_server import tools

NAME = "banking_server"


def build_server(host=config.HOST, port=config.PORTS[NAME]):
    mcp = FastMCP(NAME, host=host, port=port)
    mcp.tool()(tools.get_account_summary)
    mcp.tool()(tools.get_transaction_history)
    mcp.tool()(tools.get_linked_accounts)
    mcp.resource("account://{account_id}/summary")(tools.account_summary_resource)
    return mcp
