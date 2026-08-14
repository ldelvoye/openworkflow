import json

import httpx
import pytest

from oflow.core.contract import AuthExpired, Malformed, Unavailable
from oflow.core.mcp import MCP_PROTOCOL_VERSION, McpClient

ENDPOINT = "https://example.invalid/mcp"


def sse(payload: dict) -> httpx.Response:
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def tool_payload(payload: dict) -> httpx.Response:
    return sse({"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}})


def client_for(handler) -> McpClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return McpClient(ENDPOINT, "token-abc", http)


def test_call_tool_unwraps_sse_envelope_and_text_block():
    def handler(request):
        return tool_payload({"issues": [{"id": "ENG-1"}], "hasNextPage": False})

    result = client_for(handler).call_tool("list_issues", {"limit": 1})

    assert result == {"issues": [{"id": "ENG-1"}], "hasNextPage": False}


def test_call_tool_sends_bearer_token_and_protocol_version():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["version"] = request.headers.get("mcp-protocol-version")
        seen["accept"] = request.headers.get("accept")
        seen["body"] = json.loads(request.content)
        return tool_payload({"issues": []})

    client_for(handler).call_tool("list_issues", {"limit": 1})

    assert seen["auth"] == "Bearer token-abc"
    assert seen["version"] == MCP_PROTOCOL_VERSION
    assert "text/event-stream" in seen["accept"]
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {"name": "list_issues", "arguments": {"limit": 1}}


def test_initialize_then_call_sends_the_notification():
    methods = []

    def handler(request):
        method = json.loads(request.content)["method"]
        methods.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return sse({"result": {"protocolVersion": MCP_PROTOCOL_VERSION}})
        return tool_payload({"issues": []})

    client = client_for(handler)
    client.initialize()
    client.call_tool("list_issues", {})

    assert methods == ["initialize", "notifications/initialized", "tools/call"]


def test_the_negotiated_version_is_used_on_later_requests():
    versions = []

    def handler(request):
        versions.append(request.headers.get("mcp-protocol-version"))
        method = json.loads(request.content)["method"]
        if method == "initialize":
            return sse({"result": {"protocolVersion": "2025-06-18"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        return tool_payload({"issues": []})

    client = client_for(handler)
    client.initialize()
    client.call_tool("list_issues", {})

    assert versions[0] == MCP_PROTOCOL_VERSION
    assert versions[-1] == "2025-06-18"


def test_an_unreadable_initialize_response_keeps_the_default_version():
    versions = []

    def handler(request):
        versions.append(request.headers.get("mcp-protocol-version"))
        if json.loads(request.content)["method"] == "initialize":
            return httpx.Response(200, content=b"")
        return tool_payload({"issues": []})

    client = client_for(handler)
    client.initialize()
    client.call_tool("list_issues", {})

    assert versions[-1] == MCP_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"result": {"content": ["oops"]}}, id="content-block-is-a-string"),
        pytest.param({"error": "bad"}, id="error-is-a-string"),
        pytest.param({"result": ["nope"]}, id="result-is-a-list"),
        pytest.param({"result": {"content": [{"text": {"a": 1}}]}}, id="text-is-an-object"),
        pytest.param({"result": {"content": "oops"}}, id="content-is-a-string"),
        pytest.param({"result": {}}, id="no-content-key"),
    ],
)
def test_a_type_confused_response_is_malformed_not_a_crash(envelope):
    """Shape is as untrusted as content: a wrong type must break one tab, not the app."""

    def handler(request):
        return sse(envelope)

    with pytest.raises(Malformed):
        client_for(handler).call_tool("list_issues", {})


def test_a_type_confused_initialize_does_not_crash():
    def handler(request):
        if json.loads(request.content)["method"] == "initialize":
            return sse({"result": "nope"})
        return httpx.Response(202)

    client_for(handler).initialize()


def test_an_unsupported_negotiated_version_is_refused():
    def handler(request):
        if json.loads(request.content)["method"] == "initialize":
            return sse({"result": {"protocolVersion": "2026-07-28"}})
        return httpx.Response(202)

    with pytest.raises(Malformed, match="2026-07-28"):
        client_for(handler).initialize()


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("http://example.invalid/mcp", id="remote-plaintext"),
        pytest.param("http://localhost:9000/mcp", id="localhost-is-a-name-not-an-address"),
        pytest.param("ftp://example.invalid/mcp", id="unknown-scheme"),
    ],
)
def test_an_endpoint_that_could_expose_the_token_is_refused(endpoint):
    with pytest.raises(Malformed, match="non-https"):
        McpClient(endpoint, "token-abc", httpx.Client())


@pytest.mark.parametrize(
    "endpoint",
    ["https://example.invalid/mcp", "http://127.0.0.1:9000/mcp", "http://[::1]:9000/mcp"],
)
def test_https_and_loopback_addresses_are_allowed(endpoint):
    """Plaintext that never leaves the machine is not exposed, and local MCP
    servers are a normal deployment."""
    McpClient(endpoint, "token-abc", httpx.Client())


def test_server_error_text_cannot_carry_terminal_escapes():
    def handler(request):
        return sse({"error": {"message": "bad\x1b[31m" + "x" * 500}})

    with pytest.raises(Malformed) as excinfo:
        client_for(handler).call_tool("list_issues", {})

    message = str(excinfo.value)
    assert "\x1b" not in message
    assert len(message) < 200


def test_a_401_becomes_auth_expired():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_token"})

    with pytest.raises(AuthExpired):
        client_for(handler).call_tool("list_issues", {})


def test_a_500_becomes_unavailable():
    def handler(request):
        return httpx.Response(500, text="upstream is sad")

    with pytest.raises(Unavailable):
        client_for(handler).call_tool("list_issues", {})


def test_a_transport_failure_becomes_unavailable():
    def handler(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(Unavailable):
        client_for(handler).call_tool("list_issues", {})


def test_a_jsonrpc_error_becomes_malformed():
    def handler(request):
        return sse({"error": {"code": -32602, "message": "unknown tool"}})

    with pytest.raises(Malformed, match="unknown tool"):
        client_for(handler).call_tool("nope", {})


def test_a_non_sse_body_becomes_malformed():
    def handler(request):
        return httpx.Response(200, content=b"<html>proxy</html>")

    with pytest.raises(Malformed):
        client_for(handler).call_tool("list_issues", {})


def test_a_text_block_that_is_not_json_becomes_malformed():
    def handler(request):
        return sse({"result": {"content": [{"type": "text", "text": "here are your issues!"}]}})

    with pytest.raises(Malformed):
        client_for(handler).call_tool("list_issues", {})


def test_an_empty_content_list_becomes_malformed():
    def handler(request):
        return sse({"result": {"content": []}})

    with pytest.raises(Malformed):
        client_for(handler).call_tool("list_issues", {})


def test_errors_never_contain_the_token():
    def handler(request):
        return httpx.Response(500, text="upstream is sad")

    with pytest.raises(Unavailable) as excinfo:
        client_for(handler).call_tool("list_issues", {})
    assert "token-abc" not in str(excinfo.value)
