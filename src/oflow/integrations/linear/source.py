"""Fetch issues from Linear's MCP endpoint and map them to typed items.

Never formats. The panel decides how any of this looks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from oflow.auth.store import Credentials
from oflow.core.contract import Item, Malformed, Unavailable
from oflow.core.mcp import McpClient
from oflow.core.text import printable, printable_block

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

# The one negotiated-version cache for this process: learned on the first
# fetch, dropped when a call fails without a handshake (see _Session.call).
_negotiated_version: str | None = None


class _Session:
    """One fetch's MCP client, with the handshake skipped when the negotiated
    version is already known — and redone once if that optimism turns out wrong.
    """

    def __init__(self, token: str, http: httpx.Client) -> None:
        global _negotiated_version
        self._skipped_handshake = _negotiated_version is not None
        self._client = McpClient(ENDPOINT, token, http, version=_negotiated_version)
        if not self._skipped_handshake:
            self._client.initialize()
            _negotiated_version = self._client.version

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        global _negotiated_version
        try:
            return self._client.call_tool(name, arguments)
        except (Malformed, Unavailable):
            if not self._skipped_handshake:
                raise
            # The skipped handshake may itself be the failure; pay for a full
            # one and retry once. AuthExpired is deliberately not recovered:
            # a rejected token is not something a handshake can fix.
            _negotiated_version = None
            self._skipped_handshake = False
            self._client.initialize()
            _negotiated_version = self._client.version
            return self._client.call_tool(name, arguments)


@dataclass(frozen=True)
class Issue(Item):
    title: str
    status: str
    status_type: str
    team: str
    priority: str


COMMENT_LIMIT = 5


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class IssueDetail:
    description: str
    assignee: str
    comments: tuple[Comment, ...]


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
    session = _Session(credentials.access_token, http)

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
        payload = session.call("list_issues", arguments)
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


def _string(raw: dict[str, Any], key: str) -> str:
    """A field the panel renders unconditionally, so absent or non-str is Malformed.

    ``.get`` rather than ``[]``: a missing key and an explicit ``null`` both
    fail the isinstance check the same way, so one branch covers both.
    """
    value = raw.get(key)
    if not isinstance(value, str):
        raise Malformed(f"{key!r} was {type(value).__name__}, expected a string")
    return value


def _optional_string(raw: dict[str, Any], key: str) -> str:
    """A field the panel treats as optional: absent or null defaults to "",
    but a present value of the wrong type still means the server's shape
    cannot be trusted.
    """
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Malformed(f"{key!r} was {type(value).__name__}, expected a string")
    return value


def _priority_name(raw: dict[str, Any]) -> str:
    priority = raw.get("priority")
    if priority is None:
        return ""
    if not isinstance(priority, dict):
        raise Malformed(f"'priority' was {type(priority).__name__}, expected an object")
    return _optional_string(priority, "name")


def _issue_of(raw: Any) -> Issue:
    if not isinstance(raw, dict):
        raise Malformed(f"an issue was {type(raw).__name__}, expected an object")
    try:
        updated_at = datetime.fromisoformat(raw["updatedAt"])
    except (KeyError, TypeError, ValueError) as error:
        raise Malformed(
            f"an issue did not match the expected shape ({printable(str(error))})"
        ) from error

    return Issue(
        id=_string(raw, "id"),
        updated_at=updated_at,
        url=_string(raw, "url"),
        title=_string(raw, "title"),
        status=_string(raw, "status"),
        status_type=_string(raw, "statusType"),
        team=_optional_string(raw, "team"),
        priority=_priority_name(raw),
    )


def fetch_detail(credentials: Credentials, http: httpx.Client, item: Item) -> IssueDetail:
    """The selected issue's expanded view: description, assignee, newest
    comments (oldest first, so reading order matches the thread)."""
    session = _Session(credentials.access_token, http)
    issue_payload = session.call("get_issue", {"id": item.id})
    comments_payload = session.call("list_comments", {"issueId": item.id, "limit": 25})
    raw_comments = comments_payload.get("comments")
    if not isinstance(raw_comments, list):
        raise Malformed("list_comments returned no comment list")
    comments = sorted(
        (_comment_of(raw) for raw in raw_comments), key=lambda comment: comment.created_at
    )
    assignee = _optional_string(issue_payload, "assignee")
    return IssueDetail(
        description=printable_block(_optional_string(issue_payload, "description")),
        assignee=printable(assignee) if assignee else "",
        comments=tuple(comments[-COMMENT_LIMIT:]),
    )


def _comment_of(raw: Any) -> Comment:
    if not isinstance(raw, dict):
        raise Malformed(f"a comment was {type(raw).__name__}, expected an object")
    author = raw.get("author")
    if author is None:
        name = ""
    elif isinstance(author, dict):
        raw_name = _optional_string(author, "name")
        name = printable(raw_name) if raw_name else ""
    else:
        raise Malformed(f"'author' was {type(author).__name__}, expected an object")
    try:
        created_at = datetime.fromisoformat(raw["createdAt"])
    except (KeyError, TypeError, ValueError) as error:
        raise Malformed(
            f"a comment did not match the expected shape ({printable(str(error))})"
        ) from error
    return Comment(
        author=name, body=printable_block(_string(raw, "body"), limit=2000), created_at=created_at
    )
