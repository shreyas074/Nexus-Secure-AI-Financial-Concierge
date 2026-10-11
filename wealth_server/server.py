from mcp.server.fastmcp import FastMCP

import config
from wealth_server import tools

NAME = "wealth_server"


def build_server(host=config.HOST, port=config.PORTS[NAME]):
    mcp = FastMCP(NAME, host=host, port=port)
    mcp.tool()(tools.get_portfolio_summary)
    mcp.tool()(tools.get_investment_product_details)
    mcp.tool()(tools.check_product_suitability)
    return mcp
