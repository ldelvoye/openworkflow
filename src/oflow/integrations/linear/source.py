"""Fetch issues from Linear's MCP endpoint and map them to typed items.

Never formats. The panel decides how any of this looks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from oflow.auth.store import Credentials
from oflow.core.contract import Item, Malformed, Unavailable
from oflow.core.mcp import McpClient
from oflow.core.text import capped, printable, printable_block

ENDPOINT = "https://mcp.linear.app/mcp"

# Linear embeds machine tags in descriptions and comment bodies, e.g.
# <issue id="..." href="https://linear.app/...">ENG-123</issue>. Only these
# four known names are touched, so unrelated angle-bracket text (a code
# fence's own literal HTML, say) is left alone.
_LINEAR_TAG_NAMES = ("issue", "user", "project", "document")
_LINEAR_PAIRED_TAG = re.compile(
    r"<(" + "|".join(_LINEAR_TAG_NAMES) + r")\b([^>]*)>(.*?)</\1>", re.DOTALL
)
_LINEAR_LONE_TAG = re.compile(r"<(?:" + "|".join(_LINEAR_TAG_NAMES) + r")\b[^>]*/?>")
_HREF_ATTR = re.compile(r'href="([^"]*)"')


def _rewrite_paired_tag(match: re.Match[str]) -> str:
    attributes, inner = match.group(2), match.group(3)
    href_match = _HREF_ATTR.search(attributes)
    if href_match is None:
        return inner
    href = href_match.group(1)
    # Only a well-formed https:// link becomes a markdown link — an http,
    # javascript:, or otherwise unparseable href degrades to inner text only,
    # same as a tag with no href at all.
    parsed = urlsplit(href)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return inner
    return f"[{inner}]({href})"


def _unwrap_linear_tags(text: str) -> str:
    """Rewrite a paired tag carrying a usable https:// href into a markdown
    link `[inner](href)`, keeping the inner text as the link label; a paired
    tag with no href (or an unusable one) degrades to inner text only. A lone
    opening or self-closing tag is deleted outright (a mention that lost its
    label). Hand-typed references (e.g. a plain "CTRL-2" a person typed) never
    match this pattern in the first place, so only Linear-inserted mentions
    can ever become a link — that guarantee holds by construction, not by
    checking who wrote the text.
    """
    rewritten = _LINEAR_PAIRED_TAG.sub(_rewrite_paired_tag, text)
    return _LINEAR_LONE_TAG.sub("", rewritten)


FIELDS = (
    "title",
    "status",
    "statusType",
    "updatedAt",
    "url",
    "team",
    "priority",
)

ACTIVE_STATUS_TYPES = frozenset[str]({"started", "unstarted"})

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
COMMENTS_FETCH_LIMIT = 25
DESCRIPTION_LIMIT = 50_000
COMMENT_BODY_LIMIT = 10_000


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
    # Hidden comments are comments that were fetched but dropped past COMMENT_LIMIT.
    hidden_comments: int = 0
    hidden_is_lower_bound: bool = False


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
    newest_first = sorted(active, key=lambda issue: issue.updated_at, reverse=True)
    return tuple(newest_first)


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
    comments_payload = session.call(
        "list_comments", {"issueId": item.id, "limit": COMMENTS_FETCH_LIMIT}
    )
    raw_comments = comments_payload.get("comments")
    if not isinstance(raw_comments, list):
        raise Malformed("list_comments returned no comment list")
    comments = sorted(
        (_comment_of(raw) for raw in raw_comments), key=lambda comment: comment.created_at
    )
    assignee = _optional_string(issue_payload, "assignee")
    # Sanitize uncapped, then unwrap, then cap: unwrapping after capping could
    # cut mid-tag and leave one of our own <issue>/<user>/... fragments
    # dangling in what the panel renders.
    description = capped(
        _unwrap_linear_tags(
            printable_block(_optional_string(issue_payload, "description"), limit=None)
        ),
        DESCRIPTION_LIMIT,
    )
    return IssueDetail(
        description=description,
        assignee=printable(assignee) if assignee else "",
        comments=tuple(comments[-COMMENT_LIMIT:]),
        hidden_comments=max(0, len(raw_comments) - COMMENT_LIMIT),
        hidden_is_lower_bound=len(raw_comments) >= COMMENTS_FETCH_LIMIT
        or bool(comments_payload.get("hasNextPage")),
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
    body = capped(
        _unwrap_linear_tags(printable_block(_string(raw, "body"), limit=None)), COMMENT_BODY_LIMIT
    )
    return Comment(author=name, body=body, created_at=created_at)
