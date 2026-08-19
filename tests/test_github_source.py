"""Tests for the GitHub source.

No network. PyGithub's own connection-class seam serves recorded payloads, so
everything under test — the queries issued, the mapping to items, and the
translation of failures — runs through the real client rather than a stand-in
for it.
"""

import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from github.Requester import (
    HTTPRequestsConnectionClass,
    HTTPSRequestsConnectionClass,
    Requester,
    RequestsResponse,
)

from smorg.auth.store import Credentials
from smorg.core.contract import AuthExpired, Malformed, Unavailable
from smorg.integrations.github.source import (
    BASE_QUERY,
    MAX_PER_QUERY,
    QUERIES,
    Category,
    PullRequest,
    fetch,
    fetch_detail,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "github_search.json").read_text())
PULL = json.loads((FIXTURES / "github_pull.json").read_text())
REVIEWS = json.loads((FIXTURES / "github_reviews.json").read_text())

HELLO = SEARCH["items"][0]
TOOLS = SEARCH["items"][1]

CREDENTIALS = Credentials(
    access_token="github_pat_secret", refresh_token=None, expires_at=None, scope=""
)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("PyGithub brings its own transport; the shell's client must go unused")


# Handed to every call, and wired to fail if anything ever reaches it: the
# GitHub source is the one source that does not fetch over the shared client.
UNUSED_HTTP = httpx.Client(transport=httpx.MockTransport(_refuse))

ITEM = PullRequest(
    id="octocat/hello#42",
    updated_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    url="https://github.com/octocat/hello/pull/42",
    number=42,
    title="Tidy the loader",
    repository="octocat/hello",
    author="octocat",
    category=Category.NEEDS_YOUR_REVIEW,
)


class _Recorded(RequestsResponse):
    """One recorded answer: an HTTP status and a JSON body.

    Subclasses the response type PyGithub's connection returns, so a change to
    what it has to provide shows up as a type error rather than at runtime.
    """

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._body = json.dumps(body)

    def getheaders(self):
        return self.headers.items()

    def read(self) -> str:
        return self._body

    def raise_for_status(self) -> None:
        """Never raises: a recorded error status is answered rather than
        thrown, so the source sees what a real error response looks like."""


class _Server:
    """Recorded answers keyed by what was asked for, and a log of the asking.

    Search results are registered per query string, so a test says which
    category a pull request came back under by registering it against that
    category's query and nothing else.
    """

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.paths: list[str] = []
        self._by_query: dict[str, tuple[int, object]] = {}
        self._by_path: dict[str, tuple[int, object]] = {}

    def searching(self, qualifiers: str, items: list[dict], status: int = 200) -> None:
        body = {"total_count": len(items), "incomplete_results": False, "items": items}
        self._by_query[f"{BASE_QUERY} {qualifiers}"] = (status, body)

    def failing_every_search(self, status: int, body: object) -> None:
        for _, qualifiers in QUERIES:
            self._by_query[f"{BASE_QUERY} {qualifiers}"] = (status, body)

    def serving(self, path: str, body: object, status: int = 200) -> None:
        self._by_path[path] = (status, body)

    def answer(self, url: str) -> _Recorded:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        self.paths.append(parts.path)
        if query:
            self.searches.append(query)
            status, body = self._by_query.get(
                query, (200, {"total_count": 0, "incomplete_results": False, "items": []})
            )
            return _Recorded(status, body)
        status, body = self._by_path[parts.path]
        return _Recorded(status, body)


_LIVE: list[_Server] = []


class _Answering:
    """Answers every request from the live _Server instead of a network.

    Mixed into both connection classes PyGithub injects: https is the one the
    client uses, and http is stubbed too so nothing can quietly fall through to
    a real socket. Nothing of the parent is initialised — opening a session is
    the one thing this must not do.
    """

    def __init__(self, host: str, port: int | None = None, **kwargs: object) -> None:
        self._url = ""

    def request(self, verb, url, input, headers, stream=False) -> None:
        self._url = url

    def getresponse(self) -> _Recorded:
        return _LIVE[0].answer(self._url)

    def close(self) -> None:
        pass


class _HttpsConnection(_Answering, HTTPSRequestsConnectionClass):
    pass


class _HttpConnection(_Answering, HTTPRequestsConnectionClass):
    pass


@pytest.fixture
def github():
    server = _Server()
    _LIVE.append(server)
    Requester.injectConnectionClasses(_HttpConnection, _HttpsConnection)
    try:
        yield server
    finally:
        Requester.resetConnectionClasses()
        _LIVE.clear()


# --- What gets asked for ---


def test_every_declared_category_is_searched_for(github):
    fetch(CREDENTIALS, UNUSED_HTTP)

    asked = set(github.searches)
    for _, qualifiers in QUERIES:
        assert f"{BASE_QUERY} {qualifiers}" in asked


def test_every_category_the_panel_draws_can_be_produced(github):
    """The queries and the Category enum are declared apart; a category with no
    query behind it would leave an always-empty section in the tab."""
    assert {category for category, _ in QUERIES} == set(Category)


def test_a_search_names_no_repository_so_the_whole_account_is_covered(github):
    fetch(CREDENTIALS, UNUSED_HTTP)

    assert all("repo:" not in query for query in github.searches)


# --- Which bucket a pull request lands in ---


def test_a_pull_request_keeps_the_first_category_that_claimed_it(github):
    """Both review queries match a directly-requested review; the direct one
    runs first, so the tab shows it once, on the row that is actually true."""
    github.searching("user-review-requested:@me", [HELLO])
    github.searching("review-requested:@me", [HELLO])

    pulls = fetch(CREDENTIALS, UNUSED_HTTP)

    assert [pull.category for pull in pulls] == [Category.NEEDS_YOUR_REVIEW]


def test_a_team_request_is_whatever_the_direct_query_did_not_claim(github):
    """`review-requested:@me` is a superset covering both kinds; subtracting
    the direct ones by precedence is what leaves the team's."""
    github.searching("user-review-requested:@me", [HELLO])
    github.searching("review-requested:@me", [HELLO, TOOLS])

    by_id = {pull.id: pull.category for pull in fetch(CREDENTIALS, UNUSED_HTTP)}

    assert by_id["octocat/hello#42"] is Category.NEEDS_YOUR_REVIEW
    assert by_id["octocat/tools#7"] is Category.NEEDS_TEAM_REVIEW


def test_a_pull_request_waiting_on_you_outranks_the_catch_all(github):
    github.searching("author:@me draft:false review:changes_requested", [HELLO])
    github.searching("author:@me draft:false", [HELLO, TOOLS])

    by_id = {pull.id: pull.category for pull in fetch(CREDENTIALS, UNUSED_HTTP)}

    assert by_id["octocat/hello#42"] is Category.NEEDS_ACTION
    assert by_id["octocat/tools#7"] is Category.WAITING


def test_a_failing_check_is_something_to_act_on(github):
    github.searching("author:@me draft:false status:failure", [HELLO])

    pulls = fetch(CREDENTIALS, UNUSED_HTTP)

    assert [pull.category for pull in pulls] == [Category.NEEDS_ACTION]


# --- What an item carries ---


def test_an_item_is_identified_by_repository_and_number(github):
    """Unique across every repository in the tab and stable across refreshes,
    which is what the seen-state keys off."""
    github.searching("user-review-requested:@me", [HELLO])

    assert fetch(CREDENTIALS, UNUSED_HTTP)[0].id == "octocat/hello#42"


def test_an_item_carries_what_the_panel_draws(github):
    github.searching("user-review-requested:@me", [HELLO])

    pull = fetch(CREDENTIALS, UNUSED_HTTP)[0]

    assert pull.repository == "octocat/hello"
    assert pull.number == 42
    assert pull.title == "Tidy the loader"
    assert pull.author == "octocat"
    assert pull.url == "https://github.com/octocat/hello/pull/42"
    assert pull.updated_at == datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_pull_requests_come_back_newest_first(github):
    github.searching("user-review-requested:@me", [TOOLS, HELLO])

    pulls = fetch(CREDENTIALS, UNUSED_HTTP)

    assert [pull.id for pull in pulls] == ["octocat/hello#42", "octocat/tools#7"]


def test_a_title_carrying_terminal_escapes_is_sanitised(github):
    """A pull request title is somebody else's text arriving at a terminal."""
    hostile = HELLO | {"title": "Tidy\x1b[31m the\x00 loader"}
    github.searching("user-review-requested:@me", [hostile])

    title = fetch(CREDENTIALS, UNUSED_HTTP)[0].title

    assert "\x1b" not in title
    assert "\x00" not in title


def test_a_deleted_author_leaves_the_row_drawable(github):
    github.searching("user-review-requested:@me", [HELLO | {"user": None}])

    assert fetch(CREDENTIALS, UNUSED_HTTP)[0].author == ""


def test_the_search_stops_at_the_bound(github):
    """A dashboard is not a backlog viewer; unbounded paging would keep a
    refresh going for as long as the account has pull requests."""
    many = [HELLO | {"number": index, "id": index} for index in range(MAX_PER_QUERY + 20)]
    github.searching("user-review-requested:@me", many)

    assert len(fetch(CREDENTIALS, UNUSED_HTTP)) == MAX_PER_QUERY


# --- Failures cross the seam as one of the three ---


def test_a_rejected_token_is_auth_expired(github):
    github.failing_every_search(401, {"message": "Bad credentials"})

    with pytest.raises(AuthExpired):
        fetch(CREDENTIALS, UNUSED_HTTP)


def test_a_token_missing_a_scope_is_auth_expired_too(github):
    """A 403 here is a scope or an SSO policy, not a blip: retrying returns the
    same refusal, and re-connecting is the only thing that helps."""
    github.failing_every_search(
        403, {"message": "Resource not accessible by personal access token"}
    )

    with pytest.raises(AuthExpired):
        fetch(CREDENTIALS, UNUSED_HTTP)


def test_a_refused_query_is_malformed(github):
    """422 means a qualifier this build wrote moved under us — the tab is
    broken, and stale data would promise a recovery that is not coming."""
    github.failing_every_search(422, {"message": "Validation Failed"})

    with pytest.raises(Malformed):
        fetch(CREDENTIALS, UNUSED_HTTP)


def test_github_being_down_is_unavailable(github):
    github.failing_every_search(503, {"message": "Service unavailable"})

    with pytest.raises(Unavailable):
        fetch(CREDENTIALS, UNUSED_HTTP)


def test_a_result_naming_no_repository_is_malformed(github):
    github.searching("user-review-requested:@me", [HELLO | {"repository_url": "nonsense"}])

    with pytest.raises(Malformed):
        fetch(CREDENTIALS, UNUSED_HTTP)


def test_a_failure_never_repeats_the_token(github):
    github.failing_every_search(401, {"message": "Bad credentials"})

    with pytest.raises(AuthExpired) as raised:
        fetch(CREDENTIALS, UNUSED_HTTP)

    assert "github_pat_secret" not in str(raised.value)


def test_an_expired_token_says_so_where_the_shell_appends_the_fix(github):
    """The shell appends "run: smorg connect github" to this; the message has
    to be the half that explains why."""
    github.failing_every_search(401, {"message": "Bad credentials"})

    with pytest.raises(AuthExpired) as raised:
        fetch(CREDENTIALS, UNUSED_HTTP)

    assert "expired" in str(raised.value)


# --- The detail pane ---


def test_detail_carries_the_body_the_branches_and_the_reviews(github):
    github.serving("/repos/octocat/hello/pulls/42", PULL)
    github.serving("/repos/octocat/hello/pulls/42/reviews", REVIEWS)

    detail = fetch_detail(CREDENTIALS, UNUSED_HTTP, ITEM)

    assert "Splits the loader in two." in detail.body
    assert detail.base == "main"
    assert detail.head == "tidy-loader"
    assert [review.author for review in detail.reviews] == ["hubot", "monalisa"]
    assert [review.state for review in detail.reviews] == ["CHANGES_REQUESTED", "APPROVED"]


def test_detail_costs_one_request_per_thing_it_shows(github):
    """The repository is addressed by name and never read, so it is not
    fetched — a detail pane that cost three requests to open would make the
    key that opens it feel like a page load."""
    github.serving("/repos/octocat/hello/pulls/42", PULL)
    github.serving("/repos/octocat/hello/pulls/42/reviews", REVIEWS)

    fetch_detail(CREDENTIALS, UNUSED_HTTP, ITEM)

    assert sorted(github.paths) == [
        "/repos/octocat/hello/pulls/42",
        "/repos/octocat/hello/pulls/42/reviews",
    ]


def test_a_body_carrying_terminal_escapes_is_sanitised_without_losing_its_lines(github):
    """Dropping the escape byte is what makes the sequence inert; the "[31m"
    left behind is literal text a terminal draws rather than obeys. Newlines
    survive, since a description is rendered as markdown."""
    github.serving("/repos/octocat/hello/pulls/42", PULL | {"body": "one\x1b[31m\ntwo\x00"})
    github.serving("/repos/octocat/hello/pulls/42/reviews", [])

    body = fetch_detail(CREDENTIALS, UNUSED_HTTP, ITEM).body

    assert "\x1b" not in body
    assert "\x00" not in body
    assert len(body.splitlines()) == 2


def test_a_pull_request_with_no_body_reads_as_empty_not_missing(github):
    github.serving("/repos/octocat/hello/pulls/42", PULL | {"body": None})
    github.serving("/repos/octocat/hello/pulls/42/reviews", [])

    assert fetch_detail(CREDENTIALS, UNUSED_HTTP, ITEM).body == ""


def test_a_detail_failure_is_an_integration_error(github):
    github.serving("/repos/octocat/hello/pulls/42", {"message": "Bad credentials"}, status=401)
    github.serving("/repos/octocat/hello/pulls/42/reviews", [])

    with pytest.raises(AuthExpired):
        fetch_detail(CREDENTIALS, UNUSED_HTTP, ITEM)
