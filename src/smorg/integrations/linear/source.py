"""Fetch issues from Linear's MCP endpoint and map them to typed items."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from smorg.auth.store import Credentials
from smorg.core.contract import Item, Malformed
from smorg.core.mcp import McpSession
from smorg.core.shape import optional_string, required_string, timestamp
from smorg.core.text import sanitize_block, sanitize_line, truncate

ENDPOINT = "https://mcp.linear.app/mcp"

# Linear embeds machine tags in descriptions and comment bodies, e.g.
# <issue id="..." href="https://linear.app/...">ENG-123</issue>. Only these four known names are
# touched, so unrelated angle-bracket text (a code fence's own literal HTML, say) is left alone.
_LINEAR_TAG_NAMES = ("issue", "user", "project", "document")
_LINEAR_PAIRED_TAG = re.compile(
    r"<(" + "|".join(_LINEAR_TAG_NAMES) + r")\b([^>]*)>(.*?)</\1>", re.DOTALL
)
_LINEAR_LONE_TAG = re.compile(r"<(?:" + "|".join(_LINEAR_TAG_NAMES) + r")\b[^>]*/?>")
_HREF_ATTR = re.compile(r'href="([^"]*)"')

FIELDS = (
    "title",
    "status",
    "statusType",
    "updatedAt",
    "url",
    "team",
    "priority",
)

ACTIVE_STATUS_TYPES = frozenset({"started", "unstarted"})

MAX_PAGES = 10

COMMENT_LIMIT = 5
COMMENTS_FETCH_LIMIT = 25
DESCRIPTION_LIMIT = 50_000
COMMENT_BODY_LIMIT = 10_000


@dataclass(frozen=True)
class Issue(Item):
    title: str
    status: str
    status_type: str
    team: str
    priority: str


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
    # Comments that were fetched and dropped past COMMENT_LIMIT.
    hidden_comments: int = 0
    hidden_is_lower_bound: bool = False


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
    session = McpSession(ENDPOINT, credentials.access_token, http)

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


def _priority_of(raw: dict[str, Any]) -> str:
    priority = raw.get("priority")
    if priority is None:
        return ""
    if not isinstance(priority, dict):
        raise Malformed(f"'priority' was {type(priority).__name__}, expected an object")
    return optional_string(priority, "name")


def _issue_of(raw: Any) -> Issue:
    if not isinstance(raw, dict):
        raise Malformed(f"an issue was {type(raw).__name__}, expected an object")
    return Issue(
        id=required_string(raw, "id"),
        updated_at=timestamp(raw, "updatedAt"),
        url=required_string(raw, "url"),
        title=required_string(raw, "title"),
        status=required_string(raw, "status"),
        status_type=required_string(raw, "statusType"),
        team=optional_string(raw, "team"),
        priority=_priority_of(raw),
    )


def fetch_detail(credentials: Credentials, http: httpx.Client, item: Item) -> IssueDetail:
    """The issue's expanded view: description, assignee, and the newest comments."""
    session = McpSession(ENDPOINT, credentials.access_token, http)
    issue_payload = session.call("get_issue", {"id": item.id})
    comments_payload = session.call(
        "list_comments", {"issueId": item.id, "limit": COMMENTS_FETCH_LIMIT}
    )
    raw_comments = comments_payload.get("comments")
    if not isinstance(raw_comments, list):
        raise Malformed("list_comments returned no comment list")
    all_comments = [_comment_of(raw) for raw in raw_comments]
    oldest_first = sorted(all_comments, key=lambda comment: comment.created_at)
    newest = oldest_first[-COMMENT_LIMIT:]
    # Sanitize uncapped, then unwrap, then cap: unwrapping after capping could cut mid-tag and
    # leave one of our own <issue>/<user>/... fragments dangling in what the panel renders.
    sanitized = sanitize_block(optional_string(issue_payload, "description"), limit=None)
    description = truncate(_unwrap_linear_tags(sanitized), DESCRIPTION_LIMIT)
    assignee = optional_string(issue_payload, "assignee")
    if assignee:
        assignee_name = sanitize_line(assignee)
    else:
        assignee_name = ""
    more_on_server = bool(comments_payload.get("hasNextPage"))
    return IssueDetail(
        description=description,
        assignee=assignee_name,
        comments=tuple(newest),
        hidden_comments=max(0, len(raw_comments) - COMMENT_LIMIT),
        hidden_is_lower_bound=len(raw_comments) >= COMMENTS_FETCH_LIMIT or more_on_server,
    )


def _comment_of(raw: Any) -> Comment:
    if not isinstance(raw, dict):
        raise Malformed(f"a comment was {type(raw).__name__}, expected an object")
    author = raw.get("author")
    if author is None:
        name = ""
    elif isinstance(author, dict):
        raw_name = optional_string(author, "name")
        name = sanitize_line(raw_name) if raw_name else ""
    else:
        raise Malformed(f"'author' was {type(author).__name__}, expected an object")
    created_at = timestamp(raw, "createdAt")
    sanitized = sanitize_block(required_string(raw, "body"), limit=None)
    body = truncate(_unwrap_linear_tags(sanitized), COMMENT_BODY_LIMIT)
    return Comment(author=name, body=body, created_at=created_at)


def _unwrap_linear_tags(text: str) -> str:
    """Paired tags carrying a usable https href become markdown links labeled with their inner
    text; other paired tags degrade to the inner text alone, and lone tags are deleted.
    """
    rewritten = _LINEAR_PAIRED_TAG.sub(_rewrite_paired_tag, text)
    return _LINEAR_LONE_TAG.sub("", rewritten)


def _rewrite_paired_tag(match: re.Match[str]) -> str:
    attributes = match.group(2)
    inner = match.group(3)
    href_match = _HREF_ATTR.search(attributes)
    if href_match is None:
        return inner
    href = href_match.group(1)
    parsed = urlsplit(href)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return inner
    return f"[{inner}]({href})"
