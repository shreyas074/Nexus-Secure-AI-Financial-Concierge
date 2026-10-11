"""Ports, URLs and switches in one place."""
import os

HOST = "127.0.0.1"

# server name -> port. The orchestrator itself is exposed as a 5th MCP process.
PORTS = {
    "banking_server": 8001,
    "insurance_server": 8002,
    "wealth_server": 8003,
    "concierge_ops_server": 8004,
    "nexus_orchestrator": 8005,
}
BACKEND_SERVERS = ["banking_server", "insurance_server", "wealth_server", "concierge_ops_server"]


def server_url(name):
    return os.getenv(f"NEXUS_URL_{name.upper()}", f"http://{HOST}:{PORTS[name]}/mcp")


def llm_mode():
    """'offline' (default, deterministic, no API key) or 'claude' (CrewAI agents + Claude)."""
    return os.getenv("NEXUS_LLM", "offline").lower()
