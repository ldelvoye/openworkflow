"""JSON-RPC over HTTP against an MCP server.

Nothing here knows which service is on the other end or what any tool returns.
It removes three layers of wrapping — SSE framing, the JSON-RPC envelope, and a
JSON string inside a text content block — and hands back the decoded payload.

The absence of structuredContent is why that third layer exists: servers are free
to return prose in a text block, so a payload that does not parse is a Malformed
tab rather than an exception nobody expected. Every response is treated as
untrusted shape as well as untrusted content — a field that should be an object
may be a string, and that must degrade one tab, not stop the app.

Targets protocol 2025-11-25. See docs/mcp-protocol.md for why that version, how
to recognise a server that has moved past it, and what upgrading costs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from oflow.core.contract import AuthExpired, Malformed, Unavailable
from oflow.core.text import printable

# What we ask for. Deliberately a version we have verified rather than the newest
# published one: a server that does not recognise the requested version may reject
# the request outright instead of negotiating down, so optimism breaks connections.
MCP_PROTOCOL_VERSION = "2025-11-25"

# Revisions this client's request shape actually works against. Anything
# outside this range needs different code, not just a different string —
# earlier versions lack Streamable HTTP; later ones drop the handshake entirely.
SUPPORTED_PROTOCOL_VERSIONS = frozenset[str]({"2025-03-26", "2025-06-18", "2025-11-25"})

# Addresses only. "localhost" is a name, so whether it stays on this machine
# depends on resolution — the one thing the exemption below assumes it never has
# to trust.
LOOPBACK_HOSTS = frozenset[str]({"127.0.0.1", "::1"})


class McpClient:
    def __init__(
        self,
        endpoint: str,
        token: str,
        http: httpx.Client,
        version: str | None = None,
    ) -> None:
        _require_private_transport(endpoint)
        self._endpoint = endpoint
        self._token = token
        self._http = http
        self._version = version or MCP_PROTOCOL_VERSION

    @property
    def version(self) -> str:
        """Protocol revision used in requests; initialize() updates it per server negotiation."""
        return self._version

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._version,
        }

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            response = self._http.post(self._endpoint, headers=self._headers(), json=body)
        except httpx.HTTPError as error:
            raise Unavailable(f"could not reach {self._endpoint}") from error
        if response.status_code in (401, 403):
            raise AuthExpired("the server rejected the stored credentials")
        if response.status_code >= 400:
            raise Unavailable(f"{self._endpoint} returned HTTP {response.status_code}")
        return response

    def initialize(self) -> None:
        """Announce the client and adopt whatever version the server names.

        This is the only way to learn which revision a server actually speaks, so
        it is load-bearing rather than a formality: a server may support an older
        version than we ask for, and every later request has to match.
        """
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "oflow", "version": "0.0.0"},
                },
            }
        )
        negotiated = _negotiated_version(response)
        if negotiated is not None and negotiated != self._version:
            if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
                raise Malformed(
                    f"the server speaks protocol {printable(negotiated, 32)}, which this "
                    f"build does not. See docs/mcp-protocol.md."
                )
            self._version = negotiated
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return _payload_of(_envelope_of(response), name)


def _require_private_transport(endpoint: str) -> None:
    """Refuse to send a bearer token anywhere it could be read in transit.

    Loopback is exempt for the same reason the OAuth redirect is: plaintext that
    never leaves the machine is not exposed, and MCP servers are commonly run
    locally.

    Raised as Malformed rather than Unavailable because the distinction the shell
    acts on is whether retrying could help. A bad endpoint is permanent, and
    showing last-good data marked stale would promise a recovery that cannot come.
    """
    parts = urlsplit(endpoint)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return
    raise Malformed(f"refusing to send credentials to a non-https endpoint: {printable(endpoint)}")


def _negotiated_version(response: httpx.Response) -> str | None:
    try:
        result = _envelope_of(response).get("result")
    except Malformed:
        # An unreadable handshake is not fatal on its own: the version we asked
        # for is the one we send, and the next request will report a real failure.
        return None
    if not isinstance(result, dict):
        return None
    version = result.get("protocolVersion")
    return version if isinstance(version, str) else None


def _envelope_of(response: httpx.Response) -> dict[str, Any]:
    body = response.text
    # A single-message SSE frame: one `data:` line carrying the JSON-RPC body.
    if "text/event-stream" in response.headers.get("content-type", ""):
        data = ""
        for line in body.splitlines():
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                break
        body = data
    try:
        envelope = json.loads(body)
    except ValueError as error:
        raise Malformed("the server did not return a JSON-RPC message") from error
    if not isinstance(envelope, dict):
        raise Malformed("the server returned a JSON-RPC message that is not an object")
    return envelope


def _payload_of(envelope: dict[str, Any], tool: str) -> dict[str, Any]:
    error = envelope.get("error")
    if error is not None:
        if isinstance(error, dict):
            detail = error.get("message", "unknown error")
        else:
            detail = error
        raise Malformed(f"{tool} failed: {printable(str(detail))}")

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise Malformed(f"{tool} returned no result object")
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise Malformed(f"{tool} returned no content")
    block = content[0]
    if not isinstance(block, dict):
        raise Malformed(f"{tool} returned a content block that is not an object")
    text = block.get("text")
    if not isinstance(text, str):
        raise Malformed(f"{tool} returned a content block carrying no text")

    try:
        payload = json.loads(text)
    except ValueError as error:
        raise Malformed(f"{tool} returned text that is not JSON") from error
    if not isinstance(payload, dict):
        raise Malformed(f"{tool} returned {type(payload).__name__}, expected an object")
    return payload
