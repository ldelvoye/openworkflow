# oflow v0 Phase 2 — Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the working authentication from Phase 1 into a keyboard-driven dashboard: `oflow run` opens a Textual app with one tab per connected integration, showing your Linear issues with changed items highlighted.

**Architecture:** A generic MCP transport (`mcp.py`) speaks JSON-RPC over HTTP and unwraps the SSE framing; `integrations/linear/source.py` turns one tool call into typed items and never formats; `integrations/linear/panel.py` renders those items and never fetches. The shell owns the tab bar, the global keymap, the four panel states, and the refresh scheduler, so every tab behaves identically no matter what its panel draws.

**Tech Stack:** Python ≥3.12, `textual` (new in this phase), `httpx`, `keyring`, `tomli-w`, `pytest`. Textual's `App.run_test()` pilot drives the UI tests; no snapshot plugin.

**Spec:** `docs/superpowers/specs/2026-08-12-oflow-design.md`

## Global Constraints

- Python floor: `requires-python = ">=3.12"`.
- One new runtime dependency this phase: `textual>=1.0`. Do not add others.
- Keyboard only. No mouse handlers, no mouse-only affordances.
- **Zero background timers.** No `set_interval`, no threads, no polling. Refresh is manual or on focus.
- Read-only. No `REMOTE` actions.
- Sources never format; panels never fetch. A test that violates this is a design failure, not a test bug.
- Every test runs without network access. `httpx.MockTransport` or constructed items, never a live call.
- Tokens never appear in logs, tracebacks, rendered output, or committed files.
- Commit messages: `type(scope): summary`, lowercase, imperative. No ticket ids. **No `Co-Authored-By` trailer naming an AI agent.**
- Work on a branch off `main`. Never commit directly to `main`.
- Each task lists what is **out of scope**. Do not implement out-of-scope items.

## What the transport spike established

Run on 2026-08-13 against `https://mcp.linear.app/mcp` with a stored token:

- A bare `tools/call` succeeds with no `initialize` handshake, and no `Mcp-Session-Id` is ever issued — the server is stateless.
- Responses are SSE-framed (`content-type: text/event-stream`) even for a single message: `event: message\ndata: {...}`.
- `result` contains only `content`; there is no `structuredContent`. The payload is a JSON string inside `content[0].text`, whose top-level keys are `issues`, `hasNextPage`, `cursor`.

**The handshake is still performed once per source instance.** It is not required today, and skipping it would save two requests at startup — but the MCP specification requires `initialize` before other requests, and relying on a server's tolerance for non-compliance is a dependency on someone else's leniency. Once per process is cheap insurance.

---

### Task 1: MCP transport

**Files:**
- Create: `src/oflow/mcp.py`, `tests/test_mcp.py`

**Interfaces:**
- Consumes: `oflow.contract.AuthExpired`, `Unavailable`, `Malformed`.
- Produces:
  - `MCP_PROTOCOL_VERSION = "2025-06-18"`
  - `McpClient(endpoint: str, token: str, http: httpx.Client)` — `initialize()` and
    `call_tool(name: str, arguments: dict) -> dict` (the decoded payload, already
    unwrapped from SSE, JSON-RPC envelope, and the text block).

**Out of scope:** tool discovery (`tools/list`), resources, prompts, notifications from the server, streaming multi-message responses, and any Linear-specific knowledge. This module must not mention an integration by name.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp.py`:

```python
import json

import httpx
import pytest

from oflow.contract import AuthExpired, Malformed, Unavailable
from oflow.mcp import MCP_PROTOCOL_VERSION, McpClient

ENDPOINT = "https://example.invalid/mcp"


def sse(payload: dict) -> httpx.Response:
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.mcp'`

- [ ] **Step 3: Write the implementation**

Create `src/oflow/mcp.py`:

```python
"""JSON-RPC over HTTP against an MCP server.

Nothing here knows which service is on the other end or what any tool returns.
It removes three layers of wrapping — SSE framing, the JSON-RPC envelope, and a
JSON string inside a text content block — and hands back the decoded payload.

The absence of structuredContent is why that third layer exists: servers are free
to return prose in a text block, so a payload that does not parse is a Malformed
tab rather than an exception nobody expected.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from oflow.contract import AuthExpired, Malformed, Unavailable

MCP_PROTOCOL_VERSION = "2025-06-18"


class McpClient:
    def __init__(self, endpoint: str, token: str, http: httpx.Client) -> None:
        self._endpoint = endpoint
        self._token = token
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
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
        """Announce the client, per the MCP specification.

        Not enforced by every server, but performing it means the client does not
        depend on a particular server's leniency.
        """
        self._post(
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


def _envelope_of(response: httpx.Response) -> dict[str, Any]:
    body = response.text
    # A single-message SSE frame: one `data:` line carrying the JSON-RPC body.
    if "text/event-stream" in response.headers.get("content-type", ""):
        body = next(
            (
                line[len("data:") :].strip()
                for line in body.splitlines()
                if line.startswith("data:")
            ),
            "",
        )
    try:
        envelope = json.loads(body)
    except ValueError as error:
        raise Malformed("the server did not return a JSON-RPC message") from error
    if not isinstance(envelope, dict):
        raise Malformed("the server returned a JSON-RPC message that is not an object")
    return envelope


def _payload_of(envelope: dict[str, Any], tool: str) -> dict[str, Any]:
    if "error" in envelope:
        message = envelope["error"].get("message", "unknown error")
        raise Malformed(f"{tool} failed: {message}")
    content = envelope.get("result", {}).get("content") or []
    if not content:
        raise Malformed(f"{tool} returned no content")
    text = content[0].get("text", "")
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise Malformed(f"{tool} returned text that is not JSON") from error
    if not isinstance(payload, dict):
        raise Malformed(f"{tool} returned {type(payload).__name__}, expected an object")
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oflow/mcp.py tests/test_mcp.py
git commit -m "feat(mcp): add a json-rpc client for mcp tool calls"
```

---

### Task 2: Item type and the Linear source

**Files:**
- Create: `src/oflow/integrations/linear/source.py`, `tests/test_linear_source.py`, `tests/fixtures/linear_issues.json`
- Modify: `src/oflow/contract.py` (add `Item`), `src/oflow/integrations/linear/manifest.py` (add `fetch`)

**Interfaces:**
- Consumes: `oflow.mcp.McpClient`, `oflow.auth.store.Credentials`.
- Produces:
  - `contract.Item(id: str, updated_at: datetime, url: str)` — frozen dataclass, the
    minimum the shell needs for change highlighting and the launch action.
  - `linear.source.Issue(Item)` — adds `title: str`, `status: str`, `status_type: str`,
    `team: str`, `priority: str`.
  - `linear.source.ENDPOINT = "https://mcp.linear.app/mcp"`
  - `linear.source.FIELDS: tuple[str, ...]`
  - `linear.source.fetch(credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]`

**Out of scope:** rendering, sorting for display (the source sorts by `updated_at`
descending and stops there), the detail pane, and any write action.

The shell only ever needs `id`, `updated_at`, and `url` from an item — that is
why `Item` carries exactly those three and integrations extend it. A shared type
with every field an integration might want would defeat the point of per-panel
rendering.

- [ ] **Step 1: Record the response fixture**

Create `tests/fixtures/linear_issues.json` with two pages, shaped exactly as the
live tool returns:

```json
{
  "page1": {
    "issues": [
      {
        "id": "INFRENG-446",
        "title": "Dual-write new_id on OrganizationMemberTeam writes",
        "status": "In Review",
        "statusType": "started",
        "updatedAt": "2026-08-12T22:35:05.790Z",
        "url": "https://linear.app/getsentry/issue/INFRENG-446/dual-write",
        "team": "Infrastructure Engineering",
        "priority": { "value": 2, "name": "High" }
      },
      {
        "id": "CTRL-8",
        "title": "Add per-cell latency SLO for the API Gateway",
        "status": "Done",
        "statusType": "completed",
        "updatedAt": "2026-08-12T23:22:41.283Z",
        "url": "https://linear.app/getsentry/issue/CTRL-8/add-per-cell-latency-slo",
        "team": "Control Plane",
        "priority": { "value": 0, "name": "No priority" }
      }
    ],
    "hasNextPage": true,
    "cursor": "cursor-1"
  },
  "page2": {
    "issues": [
      {
        "id": "INFRENG-467",
        "title": "Remove dead organizationmember_teams replication option values",
        "status": "Todo",
        "statusType": "unstarted",
        "updatedAt": "2026-08-11T22:52:53.552Z",
        "url": "https://linear.app/getsentry/issue/INFRENG-467/remove-dead",
        "team": "Infrastructure Engineering",
        "priority": { "value": 3, "name": "Medium" }
      }
    ],
    "hasNextPage": false,
    "cursor": null
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_linear_source.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from oflow.auth.store import Credentials
from oflow.contract import Malformed
from oflow.integrations.linear.source import FIELDS, Issue, fetch

PAGES = json.loads((Path(__file__).parent / "fixtures" / "linear_issues.json").read_text())
CREDENTIALS = Credentials("token-abc", None, None, "read")


def sse(payload: dict) -> httpx.Response:
    envelope = {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    return httpx.Response(
        200,
        content=f"event: message\ndata: {json.dumps(envelope)}\n\n".encode(),
        headers={"content-type": "text/event-stream"},
    )


def paging_handler(requests: list) -> callable:
    def handler(request):
        body = json.loads(request.content)
        if body["method"] != "tools/call":
            return httpx.Response(202)
        requests.append(body["params"]["arguments"])
        page = "page2" if body["params"]["arguments"].get("cursor") else "page1"
        return sse(PAGES[page])

    return handler


def fetch_with(handler) -> tuple[Issue, ...]:
    return fetch(CREDENTIALS, httpx.Client(transport=httpx.MockTransport(handler)))


def test_completed_issues_are_filtered_out():
    issues = fetch_with(paging_handler([]))
    assert [issue.id for issue in issues] == ["INFRENG-446", "INFRENG-467"]


def test_issues_are_sorted_by_updated_at_descending():
    issues = fetch_with(paging_handler([]))
    assert [issue.updated_at for issue in issues] == sorted(
        (issue.updated_at for issue in issues), reverse=True
    )


def test_fields_are_mapped_onto_the_item():
    issues = fetch_with(paging_handler([]))
    first = issues[0]

    assert first.title == "Dual-write new_id on OrganizationMemberTeam writes"
    assert first.status == "In Review"
    assert first.status_type == "started"
    assert first.team == "Infrastructure Engineering"
    assert first.priority == "High"
    assert first.url.startswith("https://linear.app/")
    assert first.updated_at == datetime(2026, 8, 12, 22, 35, 5, 790000, tzinfo=UTC)


def test_pagination_follows_the_cursor():
    seen: list = []
    fetch_with(paging_handler(seen))

    assert len(seen) == 2
    assert seen[0].get("cursor") is None
    assert seen[1]["cursor"] == "cursor-1"


def test_only_the_declared_fields_are_requested():
    seen: list = []
    fetch_with(paging_handler(seen))

    assert seen[0]["fields"] == list(FIELDS)
    assert seen[0]["assignee"] == "me"


def test_a_missing_field_is_malformed_not_a_key_error():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        return sse({"issues": [{"id": "ENG-1"}], "hasNextPage": False})

    with pytest.raises(Malformed):
        fetch_with(handler)


def test_an_unparseable_timestamp_is_malformed():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        broken = json.loads(json.dumps(PAGES["page1"]))
        broken["issues"][0]["updatedAt"] = "yesterday"
        broken["hasNextPage"] = False
        return sse(broken)

    with pytest.raises(Malformed):
        fetch_with(handler)


def test_pagination_stops_at_a_page_limit():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        # Always claims another page: without a bound this would never end.
        return sse({"issues": [], "hasNextPage": True, "cursor": "forever"})

    assert fetch_with(handler) == ()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_linear_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.integrations.linear.source'`

- [ ] **Step 4: Add `Item` to the contract**

In `src/oflow/contract.py`, add after the `Action` dataclass:

```python
@dataclass(frozen=True)
class Item:
    """The minimum the shell needs from any integration's data.

    Change highlighting keys off updated_at and the launch action opens url, so
    those two plus an identity are the whole shared vocabulary. Everything a
    panel draws beyond this belongs to the integration that defined it.
    """

    id: str
    updated_at: datetime
    url: str
```

Add `from datetime import datetime, timedelta` to the imports.

- [ ] **Step 5: Write the source**

Create `src/oflow/integrations/linear/source.py`:

```python
"""Fetch issues from Linear's MCP endpoint and map them to typed items.

Never formats. The panel decides how any of this looks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from oflow.auth.store import Credentials
from oflow.contract import Item, Malformed
from oflow.mcp import McpClient

ENDPOINT = "https://mcp.linear.app/mcp"

# Requested explicitly rather than accepting the default payload, which includes
# full issue descriptions and is far larger than a list view needs.
FIELDS = (
    "title",
    "status",
    "statusType",
    "updatedAt",
    "url",
    "team",
    "priority",
)

# statusType is the stable machine category; status is a per-team display label
# teams rename freely. Filtering keys off the former, presentation off the latter.
ACTIVE_STATUS_TYPES = frozenset({"started", "unstarted"})

# A bound on cursor-following, so a server that always claims another page
# cannot spin forever.
MAX_PAGES = 10


@dataclass(frozen=True)
class Issue(Item):
    title: str
    status: str
    status_type: str
    team: str
    priority: str


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
    client = McpClient(ENDPOINT, credentials.access_token, http)
    client.initialize()

    issues: list[Issue] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        arguments: dict[str, Any] = {
            "assignee": "me",
            "limit": 50,
            "orderBy": "updatedAt",
            "fields": list(FIELDS),
        }
        if cursor:
            arguments["cursor"] = cursor
        payload = client.call_tool("list_issues", arguments)
        issues.extend(_issue_of(raw) for raw in payload.get("issues", []))
        if not payload.get("hasNextPage"):
            break
        cursor = payload.get("cursor")
        if not cursor:
            break

    active = [issue for issue in issues if issue.status_type in ACTIVE_STATUS_TYPES]
    return tuple(sorted(active, key=lambda issue: issue.updated_at, reverse=True))


def _issue_of(raw: dict[str, Any]) -> Issue:
    try:
        return Issue(
            id=raw["id"],
            updated_at=datetime.fromisoformat(raw["updatedAt"]),
            url=raw["url"],
            title=raw["title"],
            status=raw["status"],
            status_type=raw["statusType"],
            team=raw.get("team") or "",
            priority=(raw.get("priority") or {}).get("name", ""),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise Malformed(f"an issue did not match the expected shape ({error})") from error
```

- [ ] **Step 6: Wire the source into the manifest**

In `src/oflow/integrations/linear/manifest.py`, add to the imports:

```python
import httpx

from oflow.auth.store import Credentials
from oflow.integrations.linear.source import Issue, fetch
```

and replace the `LinearIntegration` dataclass with:

```python
@dataclass(frozen=True)
class LinearIntegration:
    manifest: Manifest = MANIFEST

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
        return fetch(credentials, http)
```

In `src/oflow/contract.py`, extend the `Integration` protocol:

```python
class Integration(Protocol):
    @property
    def manifest(self) -> Manifest: ...

    def fetch(self, credentials: Credentials, http: httpx.Client) -> Sequence[Item]: ...
```

Add `from collections.abc import Sequence`, `import httpx`, and
`from oflow.auth.store import Credentials` to `contract.py`'s imports.

- [ ] **Step 7: Run the suite**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright`
Expected: all pass. `tests/test_registry.py`'s `FakeIntegration` now needs a
`fetch` method to satisfy the protocol — add one returning `()`.

- [ ] **Step 8: Commit**

```bash
git add src/oflow tests/test_linear_source.py tests/fixtures/linear_issues.json tests/test_registry.py
git commit -m "feat(linear): fetch and filter assigned issues"
```

---

### Task 3: Seen state

**Files:**
- Create: `src/oflow/state.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `oflow.config.config_dir`, `write_private_file`, `ensure_config_dir`.
- Produces:
  - `SeenState.load() -> SeenState`
  - `SeenState.is_changed(integration_id: str, item: Item) -> bool`
  - `SeenState.mark_seen(integration_id: str, item: Item) -> None`
  - `SeenState.mark_all_seen(integration_id: str, items: Iterable[Item]) -> None`
  - `SeenState.save() -> None`

**Out of scope:** pruning entries for items that no longer exist, any UI, and
committing state on a failed fetch (the shell decides when to save).

The stored value is the `updated_at` observed when an item was last opened. An
item is changed when its current `updated_at` is newer, or when nothing is
stored. That makes the highlight self-clearing: a plain unread flag would leave
an inbox permanently bold.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_state.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from oflow.contract import Item
from oflow.state import SeenState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def item(identifier: str = "ENG-1", updated_at: datetime = NOW) -> Item:
    return Item(id=identifier, updated_at=updated_at, url="https://example.invalid/1")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLOW_CONFIG_DIR", str(tmp_path / "cfg"))


def test_an_unseen_item_is_changed():
    assert SeenState.load().is_changed("linear", item()) is True


def test_a_seen_item_is_not_changed():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("linear", item()) is False


def test_an_item_updated_since_it_was_seen_is_changed_again():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("linear", item(updated_at=NOW + timedelta(minutes=1))) is True


def test_state_is_namespaced_by_integration():
    state = SeenState.load()
    state.mark_seen("linear", item())
    assert state.is_changed("sentry", item()) is True


def test_state_survives_a_round_trip():
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()

    assert SeenState.load().is_changed("linear", item()) is False


def test_mark_all_seen_clears_every_highlight():
    state = SeenState.load()
    items = [item("ENG-1"), item("ENG-2")]
    state.mark_all_seen("linear", items)

    assert [state.is_changed("linear", entry) for entry in items] == [False, False]


def test_a_corrupt_state_file_is_treated_as_empty():
    state = SeenState.load()
    state.mark_seen("linear", item())
    state.save()
    from oflow.state import state_path

    state_path().write_text("{not json")

    assert SeenState.load().is_changed("linear", item()) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.state'`

- [ ] **Step 3: Write the implementation**

Create `src/oflow/state.py`:

```python
"""Which items have changed since you last looked at them.

Stores the updated_at observed when an item was opened, not a read flag. An item
is highlighted when it has moved on since — so the highlight clears itself, and
an untouched backlog does not stay bold forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from oflow.config import config_dir, ensure_config_dir, write_private_file
from oflow.contract import Item


def state_path() -> Path:
    return config_dir() / "state.json"


class SeenState:
    def __init__(self, seen: dict[str, dict[str, str]]) -> None:
        self._seen = seen

    @classmethod
    def load(cls) -> SeenState:
        path = state_path()
        if not path.exists():
            return cls({})
        try:
            raw = json.loads(path.read_text())
        except ValueError:
            # Losing highlight history is a cosmetic setback, so a corrupt file
            # starts over rather than blocking the dashboard.
            return cls({})
        if not isinstance(raw, dict):
            return cls({})
        return cls(raw)

    def is_changed(self, integration_id: str, item: Item) -> bool:
        stamp = self._seen.get(integration_id, {}).get(item.id)
        if stamp is None:
            return True
        try:
            return item.updated_at > datetime.fromisoformat(stamp)
        except ValueError:
            return True

    def mark_seen(self, integration_id: str, item: Item) -> None:
        self._seen.setdefault(integration_id, {})[item.id] = item.updated_at.isoformat()

    def mark_all_seen(self, integration_id: str, items: Iterable[Item]) -> None:
        for item in items:
            self.mark_seen(integration_id, item)

    def save(self) -> None:
        ensure_config_dir()
        write_private_file(state_path(), json.dumps(self._seen))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/oflow/state.py tests/test_state.py
git commit -m "feat(state): highlight items that changed since you looked"
```

---

### Task 4: The shell

**Files:**
- Create: `src/oflow/shell/__init__.py`, `src/oflow/shell/app.py`, `src/oflow/shell/panel.py`, `tests/test_shell.py`
- Modify: `pyproject.toml` (add `textual>=1.0`), `src/oflow/cli.py` (add the `run` command)

**Interfaces:**
- Consumes: `oflow.registry.get_integration`, `oflow.config.load_config`,
  `oflow.auth.store.get_credentials`, `oflow.contract.{Item, AuthExpired, Unavailable, Malformed}`.
- Produces:
  - `shell.panel.PanelState` — `StrEnum` of `LOADING`, `READY`, `EMPTY`, `ERROR`, `STALE`.
  - `shell.panel.Panel(Static)` — base widget with `state: PanelState`, `items: tuple[Item, ...]`,
    `message: str`, `as_of: datetime | None`, and `render_items()` for subclasses to override.
  - `shell.app.OflowApp(tabs: tuple[str, ...])` — the Textual app.
  - `cli.run()` — `oflow run`.

**Out of scope:** the Linear panel's own rendering (Task 5), refresh scheduling
(Task 6), and the detail pane (v1). This task's app renders a fake integration.

Shell keys are declared with `priority=True`, which Textual checks ahead of the
focused widget and which a widget cannot disable by binding the same key. That is
what makes `RESERVED_KEYS` enforceable rather than a convention.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `"textual>=1.0"` to `dependencies`. Run `uv sync`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_shell.py`:

```python
from datetime import UTC, datetime

import pytest

from oflow.contract import Item
from oflow.shell.app import OflowApp
from oflow.shell.panel import Panel, PanelState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def item(identifier: str = "ENG-1") -> Item:
    return Item(id=identifier, updated_at=NOW, url="https://example.invalid/1")


def test_panel_states_are_the_four_the_design_names():
    assert {member.value for member in PanelState} >= {
        "loading",
        "empty",
        "error",
        "stale",
    }


def test_an_empty_panel_says_so_rather_than_looking_broken():
    panel = Panel()
    panel.state = PanelState.EMPTY
    assert "nothing" in panel.body_text().lower()


def test_an_error_panel_shows_the_reason():
    panel = Panel()
    panel.state = PanelState.ERROR
    panel.message = "Linear is unreachable"
    assert "Linear is unreachable" in panel.body_text()


def test_a_stale_panel_marks_when_the_data_is_from():
    panel = Panel()
    panel.state = PanelState.STALE
    panel.as_of = NOW
    panel.items = (item(),)
    assert "12:00" in panel.body_text()


def test_empty_and_error_never_render_alike():
    empty, error = Panel(), Panel()
    empty.state = PanelState.EMPTY
    error.state = PanelState.ERROR
    error.message = "boom"
    assert empty.body_text() != error.body_text()


@pytest.mark.asyncio
async def test_the_app_opens_with_a_tab_per_configured_integration():
    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        assert pilot.app.tab_ids == ("alpha", "beta")


@pytest.mark.asyncio
async def test_no_tabs_shows_the_connect_hint():
    async with OflowApp(tabs=()).run_test() as pilot:
        assert "connect" in pilot.app.empty_hint.lower()


@pytest.mark.asyncio
async def test_q_quits():
    app = OflowApp(tabs=("alpha",))
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert not app.is_running


@pytest.mark.asyncio
async def test_tab_switches_between_tabs():
    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        assert pilot.app.active_tab == "alpha"
        await pilot.press("tab")
        assert pilot.app.active_tab == "beta"
```

Add `pytest-asyncio>=0.24` to the dev dependency group and
`asyncio_mode = "auto"` under `[tool.pytest.ini_options]` so the async tests run.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oflow.shell'`

- [ ] **Step 4: Write the panel**

Create `src/oflow/shell/__init__.py` (empty file).

Create `src/oflow/shell/panel.py`:

```python
"""The shared chrome every tab renders inside.

Four states that must never look alike: a tab with nothing in it and a tab whose
fetch failed are different facts, and a dashboard that blurs them cannot be
trusted. Integrations override render_items and inherit everything else.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from textual.widgets import Static

from oflow.contract import Item


class PanelState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    EMPTY = "empty"
    ERROR = "error"
    STALE = "stale"


class Panel(Static):
    def __init__(self) -> None:
        super().__init__()
        self.state = PanelState.LOADING
        self.items: tuple[Item, ...] = ()
        self.message = ""
        self.as_of: datetime | None = None

    def render_items(self) -> str:
        """Overridden by an integration. The base draws identities only."""
        return "\n".join(item.id for item in self.items)

    def body_text(self) -> str:
        if self.state is PanelState.LOADING:
            return "loading…"
        if self.state is PanelState.EMPTY:
            return "nothing assigned to you"
        if self.state is PanelState.ERROR:
            return f"could not load: {self.message}"
        if self.state is PanelState.STALE:
            stamp = self.as_of.strftime("%H:%M") if self.as_of else "earlier"
            return f"showing data as of {stamp} — {self.message}\n{self.render_items()}"
        return self.render_items()

    def render(self) -> str:
        return self.body_text()
```

- [ ] **Step 5: Write the app**

Create `src/oflow/shell/app.py`:

```python
"""The dashboard shell: tabs, the global keymap, and nothing integration-specific."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from oflow.shell.panel import Panel


class OflowApp(App[None]):
    CSS = """
    Screen { layout: vertical; }
    """

    # priority=True is checked ahead of the focused widget, so a panel cannot
    # capture these by binding the same key.
    BINDINGS = [
        Binding("q", "quit", "quit", priority=True),
        Binding("question_mark", "help", "help", priority=True),
        Binding("tab", "next_tab", "next tab", priority=True),
        Binding("shift+tab", "previous_tab", "previous tab", priority=True),
    ]

    def __init__(self, tabs: tuple[str, ...]) -> None:
        super().__init__()
        self.tab_ids = tabs
        self.empty_hint = "no tabs configured — run: oflow connect <integration>"

    @property
    def active_tab(self) -> str | None:
        if not self.tab_ids:
            return None
        return self.query_one(TabbedContent).active or None

    def compose(self) -> ComposeResult:
        if not self.tab_ids:
            yield Vertical(Static(self.empty_hint))
            yield Footer()
            return
        with TabbedContent(initial=self.tab_ids[0]):
            for tab in self.tab_ids:
                with TabPane(tab, id=tab):
                    yield Panel()
        yield Footer()

    def _shift_tab(self, offset: int) -> None:
        if not self.tab_ids:
            return
        tabs = self.query_one(TabbedContent)
        index = self.tab_ids.index(tabs.active)
        tabs.active = self.tab_ids[(index + offset) % len(self.tab_ids)]

    def action_next_tab(self) -> None:
        self._shift_tab(1)

    def action_previous_tab(self) -> None:
        self._shift_tab(-1)

    def action_help(self) -> None:
        self.notify("tab/shift+tab switch tabs · r refresh · q quit")
```

- [ ] **Step 6: Add the run command**

In `src/oflow/cli.py`, add to the imports:

```python
from oflow.shell.app import OflowApp
```

Add the function:

```python
def _run() -> int:
    tabs = tuple(tab.integration for tab in load_config().tabs)
    OflowApp(tabs=tabs).run()
    return 0
```

Register the subparser next to the others:

```python
    subparsers.add_parser("run", help="open the dashboard")
```

and dispatch it in `main` before the `logout` fallthrough:

```python
        if args.command == "run":
            return _run()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: all pass.

- [ ] **Step 8: Verify by hand**

Run: `uv run oflow run`
Expected: a tab labelled `linear` with the base panel, `tab` switching, `q` quitting.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/oflow tests/test_shell.py
git commit -m "feat(shell): add the tabbed app and the four panel states"
```

---

### Task 5: The Linear panel

**Files:**
- Create: `src/oflow/integrations/linear/panel.py`, `tests/test_linear_panel.py`
- Modify: `src/oflow/shell/app.py` (mount an integration's panel), `src/oflow/integrations/linear/manifest.py` (expose the panel class)

**Interfaces:**
- Consumes: `oflow.shell.panel.Panel`, `linear.source.Issue`, `oflow.state.SeenState`.
- Produces:
  - `linear.panel.LinearPanel(Panel)` — groups by `status`, marks changed items,
    and handles the `o` action.
  - `LinearIntegration.panel_class` — the class the shell mounts for this tab.

**Out of scope:** the detail pane (`enter`), refresh, and any write action.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_panel.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from oflow.integrations.linear.panel import LinearPanel
from oflow.integrations.linear.source import Issue
from oflow.shell.panel import PanelState
from oflow.state import SeenState

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def issue(identifier: str = "ENG-1", status: str = "In Review") -> Issue:
    return Issue(
        id=identifier,
        updated_at=NOW,
        url=f"https://linear.app/x/issue/{identifier}",
        title=f"title of {identifier}",
        status=status,
        status_type="started",
        team="Infra",
        priority="High",
    )


def panel_with(*issues: Issue, seen: SeenState | None = None) -> LinearPanel:
    panel = LinearPanel()
    panel.state = PanelState.READY
    panel.items = issues
    panel.seen = seen or SeenState({})
    panel.integration_id = "linear"
    return panel


def test_issues_are_grouped_by_status():
    text = panel_with(issue("ENG-1", "In Review"), issue("ENG-2", "Todo")).body_text()
    assert "In Review" in text
    assert "Todo" in text


def test_the_identifier_and_title_both_appear():
    text = panel_with(issue("ENG-1")).body_text()
    assert "ENG-1" in text
    assert "title of ENG-1" in text


def test_a_changed_issue_is_marked_and_a_seen_one_is_not():
    seen = SeenState({})
    unchanged = issue("ENG-2")
    seen.mark_seen("linear", unchanged)

    text = panel_with(issue("ENG-1"), unchanged, seen=seen).body_text()
    marked = [line for line in text.splitlines() if "●" in line]

    assert any("ENG-1" in line for line in marked)
    assert not any("ENG-2" in line for line in marked)


def test_the_open_action_returns_the_url_of_the_selected_issue():
    panel = panel_with(issue("ENG-1"), issue("ENG-2"))
    panel.cursor = 1
    assert panel.selected_url() == "https://linear.app/x/issue/ENG-2"


def test_the_panel_never_fetches():
    """The seam the whole design rests on, enforced rather than trusted."""
    source = (Path("src") / "oflow" / "integrations" / "linear" / "panel.py").read_text()
    assert "httpx" not in source
    assert "McpClient" not in source
    assert "fetch" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_linear_panel.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the panel**

Create `src/oflow/integrations/linear/panel.py`:

```python
"""How Linear issues look. Never fetches anything."""

from __future__ import annotations

from oflow.integrations.linear.source import Issue
from oflow.shell.panel import Panel
from oflow.state import SeenState

CHANGED_MARK = "●"


class LinearPanel(Panel):
    def __init__(self) -> None:
        super().__init__()
        self.seen = SeenState({})
        self.integration_id = "linear"
        self.cursor = 0

    def selected_url(self) -> str | None:
        if not self.items:
            return None
        return self.items[min(self.cursor, len(self.items) - 1)].url

    def render_items(self) -> str:
        lines: list[str] = []
        current_status = ""
        for issue in self.items:
            if not isinstance(issue, Issue):
                continue
            if issue.status != current_status:
                current_status = issue.status
                lines.append(f"\n{current_status}")
            mark = CHANGED_MARK if self.seen.is_changed(self.integration_id, issue) else " "
            lines.append(f"{mark} {issue.id}  {issue.title}")
        return "\n".join(lines).strip()
```

- [ ] **Step 4: Expose the panel from the manifest**

In `src/oflow/integrations/linear/manifest.py`, import the panel and add it to
the integration:

```python
from oflow.integrations.linear.panel import LinearPanel


@dataclass(frozen=True)
class LinearIntegration:
    manifest: Manifest = MANIFEST
    panel_class: type[LinearPanel] = LinearPanel

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
        return fetch(credentials, http)
```

In `src/oflow/shell/app.py`, mount the integration's panel instead of the base
one by replacing the `TabPane` body:

```python
                with TabPane(tab, id=tab):
                    yield self._panel_for(tab)
```

and adding:

```python
    def _panel_for(self, integration_id: str) -> Panel:
        try:
            integration = get_integration(integration_id)
        except UnknownIntegration:
            # A config naming an integration this build dropped still opens; the
            # tab says so rather than the app refusing to start.
            panel = Panel()
            panel.state = PanelState.ERROR
            panel.message = f"{integration_id} is not supported by this build"
            return panel
        return integration.panel_class()
```

with `from oflow.registry import UnknownIntegration, get_integration` and
`from oflow.shell.panel import Panel, PanelState` at the top. Extend the
`Integration` protocol in `contract.py` with `panel_class`.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -v && uv run ruff check . && uv run pyright`
Expected: all pass. `FakeIntegration` in `tests/test_registry.py` needs a
`panel_class` attribute.

- [ ] **Step 6: Commit**

```bash
git add src/oflow tests/test_linear_panel.py tests/test_registry.py
git commit -m "feat(linear): render grouped issues with change marks"
```

---

### Task 6: Fetching and refresh

**Files:**
- Modify: `src/oflow/shell/app.py`, `tests/test_shell.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `OflowApp.refresh_tab(integration_id: str, force: bool) -> None`
  - Bindings for `r` (force refresh) and `o` (open the selected item).
  - `AppFocus` handling that refreshes any tab whose data is older than
    `manifest.stale_after`.

**Out of scope:** background timers of any kind, the detail pane, and a
mark-all-seen key (v1).

Only the visible tab fetches on startup. Other tabs fetch when first focused, so
opening the app costs one request no matter how many tabs are configured.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shell.py`:

```python
@pytest.mark.asyncio
async def test_only_the_visible_tab_fetches_on_startup(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=("alpha", "beta")).run_test():
        pass
    assert fetched == ["alpha"]


@pytest.mark.asyncio
async def test_r_forces_a_refresh_of_the_active_tab(monkeypatch):
    fetched: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append((integration_id, force)),
    )
    async with OflowApp(tabs=("alpha",)).run_test() as pilot:
        fetched.clear()
        await pilot.press("r")
    assert fetched == [("alpha", True)]


@pytest.mark.asyncio
async def test_switching_to_a_tab_fetches_it(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        "oflow.shell.app.OflowApp.refresh_tab",
        lambda self, integration_id, force=False: fetched.append(integration_id),
    )
    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        fetched.clear()
        await pilot.press("tab")
    assert fetched == ["beta"]


@pytest.mark.asyncio
async def test_the_app_never_schedules_a_timer(monkeypatch):
    """Zero background work is a design constraint, so it gets a test.

    Asserted by trapping the scheduling calls rather than inspecting Textual's
    internals, which would break on any refactor of theirs.
    """
    scheduled: list[str] = []
    monkeypatch.setattr(
        "textual.app.App.set_interval",
        lambda self, *args, **kwargs: scheduled.append("interval"),
    )
    monkeypatch.setattr(
        "textual.app.App.set_timer",
        lambda self, *args, **kwargs: scheduled.append("timer"),
    )

    async with OflowApp(tabs=("alpha", "beta")).run_test() as pilot:
        await pilot.press("tab")

    assert scheduled == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell.py -v`
Expected: the four new tests fail — `refresh_tab` does not exist.

- [ ] **Step 3: Implement fetching**

Add to `src/oflow/shell/app.py`:

```python
    def on_mount(self) -> None:
        if self.tab_ids:
            self.refresh_tab(self.tab_ids[0])

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_tab(event.pane.id or "")

    def on_app_focus(self) -> None:
        """The terminal regained focus; refresh whatever has gone stale.

        Fires only where the terminal reports focus. Where it does not, this
        degrades to tab-switch and manual refresh, which is enough.
        """
        if self.active_tab:
            self.refresh_tab(self.active_tab)

    def action_refresh(self) -> None:
        if self.active_tab:
            self.refresh_tab(self.active_tab, force=True)

    @work(thread=True)
    def refresh_tab(self, integration_id: str, force: bool = False) -> None:
        panel = self._panel_of(integration_id)
        if panel is None:
            return
        fetched_at = self._fetched_at.get(integration_id)
        integration = get_integration(integration_id)
        if not force and fetched_at is not None:
            if now() - fetched_at < integration.manifest.stale_after:
                return

        credentials = get_credentials(integration_id)
        if credentials is None:
            self.call_from_thread(self._show_error, panel, "not connected")
            return
        try:
            with httpx.Client(timeout=30) as http:
                items = tuple(integration.fetch(credentials, http))
        except IntegrationError as error:
            self.call_from_thread(self._show_error, panel, str(error), keep_items=True)
            return

        self._fetched_at[integration_id] = now()
        self.call_from_thread(self._show_items, panel, items)

    def _show_items(self, panel: Panel, items: tuple[Item, ...]) -> None:
        panel.items = items
        panel.state = PanelState.EMPTY if not items else PanelState.READY
        panel.as_of = now()
        panel.refresh()

    def _show_error(self, panel: Panel, message: str, keep_items: bool = False) -> None:
        panel.message = message
        # Last-good data is kept and marked stale rather than blanked: a tab that
        # empties on a network blip reads as "nothing to do", which is a lie.
        panel.state = PanelState.STALE if keep_items and panel.items else PanelState.ERROR
        panel.refresh()

    def _panel_of(self, integration_id: str) -> Panel | None:
        panes = self.query(TabPane)
        for pane in panes:
            if pane.id == integration_id:
                return pane.query_one(Panel)
        return None
```

Add to `__init__`: `self._fetched_at: dict[str, datetime] = {}`.

Add bindings:

```python
        Binding("r", "refresh", "refresh", priority=True),
        Binding("o", "open", "open in browser"),
```

and the action:

```python
    def action_open(self) -> None:
        panel = self._panel_of(self.active_tab or "")
        url = getattr(panel, "selected_url", lambda: None)()
        if url:
            webbrowser.open(url)
```

Imports needed: `webbrowser`, `httpx`, `from datetime import datetime`,
`from textual import work`, `from oflow.auth.store import get_credentials, now`,
`from oflow.contract import IntegrationError, Item`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: all pass.

- [ ] **Step 5: Verify by hand against the live service**

Run: `uv run oflow run`

Expected: the Linear tab loads your assigned issues grouped by status, with `●`
against everything you have not opened. `r` refetches. `o` opens the selected
issue in a browser. `q` quits. Nothing refreshes on its own while you watch it.

- [ ] **Step 6: Commit**

```bash
git add src/oflow/shell/app.py tests/test_shell.py
git commit -m "feat(shell): fetch on focus and on demand"
```

---

## Phase 2 exit criteria

1. `uv run oflow run` shows a Linear tab with your active issues, grouped by status.
2. Items you have not opened carry a change mark; opening one clears it, and an
   item that changes afterwards regains it.
3. `r` refetches; switching tabs fetches a stale tab; nothing fetches on a timer.
4. A failed fetch keeps the last-good data and marks it stale; an empty result and
   a failure never look alike.
5. `uv run pytest` passes with no network access, and `pyright` is clean.

## Carried into v1

- The detail pane (`enter`), which is what makes seen-state meaningful beyond
  the browser launch.
- `RESERVED_KEYS` derived from `OflowApp.BINDINGS` rather than hand-maintained.
- `is_expired` gaining a clock-skew margin, now that a refresh path exists.
- A `mark all seen` key, once there is enough history for it to matter.
