import json
from collections.abc import Callable
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


def paging_handler(requests: list) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
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
    first = fetch_with(paging_handler([]))[0]

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


def test_an_issue_that_is_not_an_object_is_malformed():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        return sse({"issues": ["not an object"], "hasNextPage": False})

    with pytest.raises(Malformed):
        fetch_with(handler)


def test_pagination_stops_at_a_page_limit():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        # Always claims another page: without a bound this would never end.
        return sse({"issues": [], "hasNextPage": True, "cursor": "forever"})

    assert fetch_with(handler) == ()
