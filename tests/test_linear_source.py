import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from smorg.auth.store import Credentials
from smorg.core.contract import Malformed
from smorg.integrations.linear.source import (
    COMMENT_BODY_LIMIT,
    DESCRIPTION_LIMIT,
    FIELDS,
    Issue,
    fetch,
)

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

    from smorg.core.contract import Unavailable

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

    from smorg.core.contract import AuthExpired

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
    from smorg.core.contract import Item
    from smorg.integrations.linear.source import fetch_detail

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


# --- Linear's own <issue>/<user>/<project>/<document> tags are unwrapped ---


def test_a_paired_tag_with_no_href_keeps_only_its_inner_text():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": 'See <issue id="i1">ENG-9</issue> for context.'
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == "See ENG-9 for context."
    assert "<issue" not in detail.description
    assert "</issue>" not in detail.description


def test_a_paired_tag_with_an_https_href_becomes_a_markdown_link():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": 'See <issue id="i1" href="https://linear.app/x/issue/ENG-9">'
        "ENG-9</issue> for context."
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == "See [ENG-9](https://linear.app/x/issue/ENG-9) for context."


@pytest.mark.parametrize(
    "href",
    [
        "http://linear.app/x/issue/ENG-9",  # not https
        "javascript:alert(1)",  # not https
        "not a url at all",  # unparseable as a real https:// URL
    ],
)
def test_a_paired_tag_with_an_unusable_href_degrades_to_inner_text_only(href: str) -> None:
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": f'See <issue id="i1" href="{href}">ENG-9</issue> for context.'
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == "See ENG-9 for context."
    assert "[ENG-9]" not in detail.description


def test_a_self_closing_linear_tag_in_the_description_is_deleted():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": 'ping <user id="u1" href="https://linear.app/x/u1"/> now.'
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert "<user" not in detail.description
    assert "ping" in detail.description and "now." in detail.description


def test_a_real_html_tag_inside_a_code_fence_survives_untouched():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": "```html\n<div>hello</div>\n```"
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert "<div>hello</div>" in detail.description


def test_comment_bodies_with_no_href_keep_only_inner_text():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["body"] = 'blocked by <issue id="i2">ENG-3</issue>'
    detail = detail_with(detail_handler({"comments": comments}))
    assert detail.comments[-1].body == "blocked by ENG-3"
    assert "<issue" not in detail.comments[-1].body


def test_comment_bodies_with_an_https_href_get_the_same_link_rewrite():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["body"] = (
        'blocked by <issue id="i2" href="https://linear.app/x/issue/ENG-3">ENG-3</issue>'
    )
    detail = detail_with(detail_handler({"comments": comments}))
    assert detail.comments[-1].body == "blocked by [ENG-3](https://linear.app/x/issue/ENG-3)"


def test_a_hand_typed_reference_gains_no_link():
    # No tag at all: a person typing "CTRL-2" themselves must never resolve
    # to a link just because it looks like an issue identifier — only text
    # Linear itself wrapped in a tag can become one.
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": "see CTRL-2 for the original report"
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == "see CTRL-2 for the original report"
    assert "[CTRL-2]" not in detail.description


def test_comments_that_are_not_a_list_are_malformed():
    with pytest.raises(Malformed):
        detail_with(detail_handler({"comments": {"comments": "nope"}}))


# --- How many older comments were fetched but dropped past COMMENT_LIMIT ---


def _synthetic_comments(count: int, has_next_page: bool = False) -> dict:
    return {
        "comments": [
            {
                "body": f"c{index}",
                "createdAt": f"2026-08-{(index % 27) + 1:02d}T10:00:00.000Z",
                "author": {"id": "u1", "name": "alice"},
            }
            for index in range(count)
        ],
        "hasNextPage": has_next_page,
    }


def test_hidden_comment_count_reflects_how_many_were_dropped_to_the_limit():
    detail = detail_with(detail_handler({"comments": _synthetic_comments(8)}))
    assert detail.hidden_comments == 3
    assert detail.hidden_is_lower_bound is False


def test_hidden_comment_count_is_exactly_one_for_the_shared_fixture():
    # The shared DETAIL fixture carries 6 raw comments, one past COMMENT_LIMIT.
    detail = detail_with(detail_handler())
    assert detail.hidden_comments == 1
    assert detail.hidden_is_lower_bound is False


def test_hidden_comment_count_is_a_lower_bound_when_the_fetch_limit_is_hit():
    detail = detail_with(detail_handler({"comments": _synthetic_comments(25)}))
    assert detail.hidden_comments == 20
    assert detail.hidden_is_lower_bound is True


def test_hidden_comment_count_is_a_lower_bound_when_the_server_reports_more_pages():
    detail = detail_with(detail_handler({"comments": _synthetic_comments(6, has_next_page=True)}))
    assert detail.hidden_is_lower_bound is True


def test_no_hidden_comments_when_everything_fetched_fits_the_limit():
    detail = detail_with(detail_handler({"comments": _synthetic_comments(3)}))
    assert detail.hidden_comments == 0
    assert detail.hidden_is_lower_bound is False


# --- Truncation: sanitize -> unwrap -> cap, so a cut can never dangle a tag ---


def test_an_over_limit_description_ends_with_the_truncation_marker():
    issue = json.loads(json.dumps(DETAIL["issue"])) | {
        "description": "x" * (DESCRIPTION_LIMIT + 500)
    }
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == "x" * DESCRIPTION_LIMIT + "\n\n… (truncated)"


def test_an_under_limit_description_is_unchanged():
    text = "a normal description, well under the cap"
    issue = json.loads(json.dumps(DETAIL["issue"])) | {"description": text}
    detail = detail_with(detail_handler({"issue": issue}))
    assert detail.description == text


def test_a_tag_dense_description_that_gets_capped_never_dangles_a_tag():
    # Capping runs *after* unwrapping now, so by the time a cut lands, every
    # real <issue>...</issue> has already collapsed to its inner text — the
    # cut has nothing tag-shaped left to slice through. If capping still ran
    # first (the bug), cutting the much longer raw markup at the same offset
    # would almost certainly land mid-tag and leave a "<issue" fragment.
    inner = "issue reference text here"
    one_tag = f'<issue id="i" href="https://linear.app/x/issue/ENG-1">{inner}</issue> '
    repeats = DESCRIPTION_LIMIT // (len(inner) + 1) + 100
    dense = one_tag * repeats
    issue = json.loads(json.dumps(DETAIL["issue"])) | {"description": dense}
    detail = detail_with(detail_handler({"issue": issue}))
    assert len(dense) > DESCRIPTION_LIMIT  # the raw payload itself needed capping
    assert "<issue" not in detail.description
    assert detail.description.endswith("\n\n… (truncated)")


def test_an_over_limit_comment_body_gets_the_same_capping_treatment():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["body"] = "y" * (COMMENT_BODY_LIMIT + 500)
    detail = detail_with(detail_handler({"comments": comments}))
    assert detail.comments[-1].body == "y" * COMMENT_BODY_LIMIT + "\n\n… (truncated)"


def test_an_under_limit_comment_body_is_unchanged():
    comments = json.loads(json.dumps(DETAIL["comments"]))
    comments["comments"][0]["body"] = "short reply"
    detail = detail_with(detail_handler({"comments": comments}))
    assert detail.comments[-1].body == "short reply"


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
