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
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raise Malformed("list_issues returned no issue list")
        issues.extend(_issue_of(raw) for raw in raw_issues)
        if not payload.get("hasNextPage"):
            break
        cursor = payload.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            break

    active = [issue for issue in issues if issue.status_type in ACTIVE_STATUS_TYPES]
    return tuple(sorted(active, key=lambda issue: issue.updated_at, reverse=True))


def _issue_of(raw: Any) -> Issue:
    if not isinstance(raw, dict):
        raise Malformed(f"an issue was {type(raw).__name__}, expected an object")
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
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise Malformed(f"an issue did not match the expected shape ({error})") from error
