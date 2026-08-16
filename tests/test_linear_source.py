import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from oflow.auth.store import Credentials
from oflow.core.contract import Malformed
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


def test_a_null_title_is_malformed_not_a_crash():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        broken = json.loads(json.dumps(PAGES["page1"]))
        broken["issues"][0]["title"] = None
        broken["hasNextPage"] = False
        return sse(broken)

    with pytest.raises(Malformed):
        fetch_with(handler)


def test_a_non_string_team_is_malformed():
    def handler(request):
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        broken = json.loads(json.dumps(PAGES["page1"]))
        broken["issues"][0]["team"] = {"id": "T1", "name": "Infra"}
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


def counting_handler(methods: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] != "tools/call":
            return httpx.Response(202)
        page = json.loads(json.dumps(PAGES["page1"]))
        page["hasNextPage"] = False
        return sse(page)

    return handler


def test_the_second_fetch_skips_the_handshake():
    methods: list[str] = []
    handler = counting_handler(methods)
    fetch_with(handler)
    fetch_with(handler)

    assert methods.count("initialize") == 1
    assert methods.count("tools/call") == 2


def test_a_failure_after_a_skipped_handshake_reinitializes_and_retries_once():
    methods: list[str] = []
    failures = {"remaining": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] != "tools/call":
            return httpx.Response(202)
        # The 2nd tools/call ever is the warm fetch's first attempt: the cold
        # fetch's own initialize also leaves methods.count("initialize") == 1
        # by the time it makes *its* first tools/call, so that count can't
        # tell the two apart — count tools/call attempts instead.
        if failures["remaining"] and methods.count("tools/call") == 2:
            failures["remaining"] -= 1
            return httpx.Response(400, text="session required")
        page = json.loads(json.dumps(PAGES["page1"]))
        page["hasNextPage"] = False
        return sse(page)

    fetch_with(handler)  # cold: full handshake
    issues = fetch_with(handler)  # warm: first call 400s, must recover

    assert issues  # the retry succeeded
    assert methods.count("initialize") == 2


def test_a_failure_after_a_real_handshake_is_not_retried():
    calls = {"tools": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(202)
        calls["tools"] += 1
        return httpx.Response(500, text="down")

    from oflow.core.contract import Unavailable

    with pytest.raises(Unavailable):
        fetch_with(handler)
    assert calls["tools"] == 1


def test_an_expired_token_is_never_retried_with_a_new_handshake():
    methods: list[str] = []
    handler_ok = counting_handler(methods)
    fetch_with(handler_ok)  # warm the cache

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(json.loads(request.content)["method"])
        return httpx.Response(401, json={"error": "invalid_token"})

    from oflow.core.contract import AuthExpired

    with pytest.raises(AuthExpired):
        fetch_with(handler)
    assert methods.count("initialize") == 1  # only the warm-up's


DETAIL = json.loads((Path(__file__).parent / "fixtures" / "linear_issue_detail.json").read_text())


def detail_handler(overrides: dict | None = None) -> Callable[[httpx.Request], httpx.Response]:
    payloads = json.loads(json.dumps(DETAIL)) | (overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] != "tools/call":
            return httpx.Response(202)
        name = body["params"]["name"]
        return sse(payloads["issue"] if name == "get_issue" else payloads["comments"])

    return handler


def detail_with(handler):
    from oflow.core.contract import Item
    from oflow.integrations.linear.source import fetch_detail

    item = Item(id="ENG-1", updated_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC), url="https://x")
    return fetch_detail(CREDENTIALS, httpx.Client(transport=httpx.MockTransport(handler)), item)


def test_detail_carries_description_assignee_and_capped_ascending_comments():
    detail = detail_with(detail_handler())
    assert detail.description == "First line.\nSecond line."
    assert detail.assignee == "Lucas Delvoye"
    assert [comment.body for comment in detail.comments] == ["c2", "c3", "c4", "middle", "newest"]
    assert detail.comments[-1].author == "alice"


def test_a_null_description_and_assignee_become_empty_strings():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {"description": None, "assignee": None}
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == ""
    assert detail.assignee == ""


def test_detail_text_is_sanitized_at_the_source():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {"description": "ok\n\x1b[31mbad\x1b[0m"}
    detail = detail_with(detail_handler({"issue": issue}))
    assert "\x1b" not in detail.description
    assert "\n" in detail.description


def test_detail_assignee_is_sanitized_at_the_source():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {"assignee": "Lu\x1b[31mcas"}
    detail = detail_with(detail_handler({"issue": issue}))
    assert "\x1b" not in detail.assignee


def test_comment_author_is_sanitized_at_the_source():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["author"] = {"id": "u1", "name": "Al\x1b[31mice"}
    detail = detail_with(detail_handler({"comments": comments}))
    assert "\x1b" not in detail.comments[-1].author


def test_comments_that_are_not_a_list_are_malformed():
    with pytest.raises(Malformed):
        detail_with(detail_handler({"comments": {"comments": "nope"}}))


def test_a_comment_without_a_body_is_malformed():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    del comments["comments"][0]["body"]
    with pytest.raises(Malformed):
        detail_with(detail_handler({"comments": comments}))


def test_a_comment_without_an_author_degrades_to_anonymous():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["author"] = None
    detail = detail_with(detail_handler({"comments": comments}))
    assert detail.comments[-1].author == ""


def test_detail_reuses_the_cached_handshake():
    methods: list[str] = []
    warm = counting_handler(methods)
    fetch_with(warm)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        if body["method"] != "tools/call":
            return httpx.Response(202)
        name = body["params"]["name"]
        return sse(DETAIL["issue"] if name == "get_issue" else DETAIL["comments"])

    detail_with(handler)
    assert methods.count("initialize") == 1
