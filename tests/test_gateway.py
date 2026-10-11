import pytest

from errors import NexusToolError, ServerUnavailableError
from nexus_orchestrator.gateway import Gateway


def test_call_returns_data_through_mcp(gateway):
    data = gateway.call("banking_server", "get_account_summary", account_id="ACC-20077", caller_scope="customer_self")
    assert data["account_ref"] == "ACC-****0077"
    assert gateway.calls == [("banking_server", "get_account_summary")]


def test_tool_level_errors_become_typed_exceptions(gateway):
    with pytest.raises(NexusToolError) as exc:
        gateway.call("banking_server", "get_account_summary", account_id="ACC-99999", caller_scope="customer_self")
    assert exc.value.code == "ACCOUNT_NOT_FOUND"
    with pytest.raises(NexusToolError) as exc:
        gateway.call("banking_server", "get_account_summary", account_id="bad", caller_scope="customer_self")
    assert exc.value.code == "INVALID_ID_FORMAT"


def test_unknown_tool_is_a_protocol_error(gateway):
    with pytest.raises(NexusToolError):
        gateway.call("banking_server", "drop_all_tables")


def test_a_server_cannot_serve_another_servers_tool(gateway):
    with pytest.raises(NexusToolError):
        gateway.call("insurance_server", "get_account_summary", account_id="ACC-20077", caller_scope="customer_self")


def test_offline_server_raises_typed_error():
    with pytest.raises(ServerUnavailableError) as exc:
        Gateway(mode="memory", down={"wealth_server"}).call("wealth_server", "get_portfolio_summary",
                                                           portfolio_id="PORT-20077", caller_scope="customer_self")
    assert exc.value.code == "SERVER_UNAVAILABLE" and exc.value.details["server"] == "wealth_server"


def test_http_mode_with_nothing_listening_is_a_typed_error():
    with pytest.raises(ServerUnavailableError):
        Gateway(mode="http", timeout=3).call("banking_server", "get_account_summary", account_id="ACC-20077",
                                             caller_scope="customer_self")


def test_resource_and_prompt_access(gateway):
    profile = gateway.read_resource("concierge_ops_server", "customer://CUS-20077/profile")
    assert profile["name"] == "Arjun Mehta"
    text = gateway.get_prompt("concierge_ops_server", "request_classification_prompt", customer_message="hi",
                              customer_tier="gold", prior_interaction_summary="none", known_product_lines="banking")
    assert "gold" in text


def test_list_primitives(gateway):
    got = gateway.list_primitives("concierge_ops_server")
    assert len(got["tools"]) == 4 and len(got["resources"]) == 2 and len(got["prompts"]) == 2
