"""Fetch pull requests from GitHub through PyGithub and map them to typed items.

Never formats. The panel decides how any of this looks.

A pull request's category is the query it matched, not something read off the
pull request afterwards. Neither a review decision nor a check result appears on
a search result, so deciding "ready to merge" per pull request would cost a
request each; one search per category costs a fixed handful no matter how many
pull requests come back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

import httpx
import requests
from github import (
    Auth,
    BadAttributeException,
    BadCredentialsException,
    Github,
    GithubException,
    RateLimitExceededException,
)
from github.GithubObject import GithubObject
from github.Issue import IssueSearchResult
from github.PaginatedList import PaginatedList
from github.PullRequest import PullRequest as GithubPullRequest
from github.PullRequestReview import PullRequestReview

from smorg.auth.store import Credentials
from smorg.core.contract import AuthExpired, IntegrationError, Item, Malformed, Unavailable
from smorg.core.text import capped, printable, printable_block

REQUEST_TIMEOUT_SECONDS = 30
RESULTS_PER_PAGE = 50

# A dashboard is not a backlog viewer, and a search this deep already means the
# tab is the wrong tool. Bounds the work one refresh can do, the same way
# Linear's MAX_PAGES does.
MAX_PER_QUERY = 50

# GitHub answers a rate-limited request with a 403 that PyGithub retries after
# sleeping. Two is enough to ride out a secondary limit; ten (its default) would
# park a refresh for minutes with the tab saying "loading…" the whole time.
MAX_RETRIES = 2


class Category(StrEnum):
    """Which of the dashboard's buckets a pull request landed in.

    Carried on the item because only `fetch` can know it — the panel has no way
    to recompute a review decision without going back to the network.
    """

    NEEDS_YOUR_REVIEW = "needs your review"
    NEEDS_TEAM_REVIEW = "needs your team's review"
    DRAFT = "drafts"
    WAITING = "waiting review or actions"
    NEEDS_ACTION = "needs actions"
    READY_TO_MERGE = "ready to merge"


@dataclass(frozen=True)
class PullRequest(Item):
    number: int
    title: str
    repository: str
    author: str
    category: Category


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    submitted_at: datetime | None


@dataclass(frozen=True)
class PullRequestDetail:
    body: str
    base: str
    head: str
    reviews: tuple[Review, ...]
    # Reviews that were fetched and dropped past REVIEW_LIMIT.
    hidden_reviews: int = 0
    hidden_is_lower_bound: bool = False


REVIEW_LIMIT = 5
REVIEWS_FETCH_LIMIT = 25
BODY_LIMIT = 50_000

# Every search starts here: open pull requests in live repositories. An archived
# repository's pull request cannot be merged or reviewed, so it is not work.
BASE_QUERY = "is:pr is:open archived:false"

# Order is precedence — a pull request keeps the category of the first query it
# matched, and is not looked at again. Two entries are deliberate supersets of
# the ones above them: `review-requested:@me` covers requests made of you and of
# your teams both, and the trailing `author:@me draft:false` covers every
# non-draft of yours. Each becomes "the rest" once the queries above have
# claimed theirs, which is how "your team's review" and "waiting" are computed
# without a second round of filtering.
QUERIES: tuple[tuple[Category, str], ...] = (
    (Category.NEEDS_YOUR_REVIEW, "user-review-requested:@me"),
    (Category.NEEDS_TEAM_REVIEW, "review-requested:@me"),
    (Category.DRAFT, "author:@me draft:true"),
    (Category.NEEDS_ACTION, "author:@me draft:false review:changes_requested"),
    (Category.NEEDS_ACTION, "author:@me draft:false status:failure"),
    (Category.READY_TO_MERGE, "author:@me draft:false review:approved status:success"),
    (Category.WAITING, "author:@me draft:false"),
)


def _message_of(error: GithubException) -> str:
    """The server's own explanation, when it sent a readable one."""
    data = error.data
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    return message if isinstance(message, str) else ""


def _translated(error: GithubException) -> IntegrationError:
    """An HTTP failure as one of the three the shell acts on, decided by the
    only question it asks: would retrying help?"""
    if error.status in (401, 403):
        # 403 here is a token missing a scope or blocked by an organisation's
        # SSO policy — permanent until the user reconnects, same as a 401.
        return AuthExpired("GitHub rejected the stored token; it may have expired or been revoked")
    if error.status == 422:
        # GitHub refused a query this build wrote, so a qualifier moved under
        # us. Retrying returns the same refusal.
        return Malformed(f"GitHub refused the search: {printable(_message_of(error))}")
    return Unavailable(f"GitHub returned HTTP {error.status}")


@contextmanager
def _github_errors() -> Iterator[None]:
    """Turn everything PyGithub and its transport raise into IntegrationError.

    Wraps attribute reads as well as calls: PyGithub fetches lazily, so an
    attribute that was not in a payload issues its own request and can fail
    exactly like an explicit one.
    """
    try:
        yield
    except BadCredentialsException as error:
        raise AuthExpired(
            "GitHub rejected the stored token; it may have expired or been revoked"
        ) from error
    except RateLimitExceededException as error:
        raise Unavailable("GitHub's rate limit is exhausted; it resets shortly") from error
    except BadAttributeException as error:
        raise Malformed(f"GitHub returned a field of an unexpected type: {error}") from error
    except GithubException as error:
        raise _translated(error) from error
    except requests.RequestException as error:
        raise Unavailable("could not reach GitHub") from error


def _client(credentials: Credentials, lazy: bool = False) -> Github:
    """A client for one call into GitHub.

    `lazy` stops an object built from an address it was handed from fetching
    its own payload before anything reads it — which is how the detail pane
    addresses a repository by name without paying a request for it.
    """
    return Github(
        auth=Auth.Token(credentials.access_token),
        timeout=REQUEST_TIMEOUT_SECONDS,
        per_page=RESULTS_PER_PAGE,
        retry=MAX_RETRIES,
        lazy=lazy,
        # PyGithub paces every request by default, which GitHub asks for
        # between writes. This app never writes, and a refresh is a bounded
        # burst of reads at most once per stale_after — pacing it would put
        # seconds on the clock of every refresh to solve a problem it cannot
        # have. Writes keep their own default pacing.
        seconds_between_requests=0,
    )


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[PullRequest, ...]:
    """Every open pull request that is yours or waiting on you, newest first.

    `http` goes unused: PyGithub brings its own transport, so the shell's shared
    httpx client has nothing to do here.
    """
    found: dict[str, PullRequest] = {}
    # Closed on the way out: a client owns a connection pool, and a dashboard
    # that refreshes every time you look at it would otherwise leave one behind
    # per refresh.
    with _client(credentials) as client:
        for category, qualifiers in QUERIES:
            for result in _search(client, f"{BASE_QUERY} {qualifiers}"):
                pull = _pull_request_of(result, category)
                # setdefault, not assignment: QUERIES is in precedence order, so
                # the first category to claim a pull request keeps it.
                found.setdefault(pull.id, pull)
    newest_first = sorted(found.values(), key=lambda pull: pull.updated_at, reverse=True)
    return tuple(newest_first)


def _first[T: GithubObject](results: PaginatedList[T], limit: int) -> list[T]:
    """Up to `limit` results. A PaginatedList fetches a page at a time as it is
    walked, so stopping the walk here is also what stops the paging."""
    found: list[T] = []
    for result in results:
        found.append(result)
        if len(found) >= limit:
            break
    return found


def _search(client: Github, query: str) -> list[IssueSearchResult]:
    with _github_errors():
        return _first(client.search_issues(query), MAX_PER_QUERY)


def _text_of(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise Malformed(f"{field!r} was {type(value).__name__}, expected a string")
    return value


def _moment_of(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise Malformed(f"{field!r} was {type(value).__name__}, expected a timestamp")
    return value


def _repository_of(repository_url: str) -> str:
    """The owner/name a repository API address points at, e.g.
    https://api.github.com/repos/octocat/hello -> octocat/hello."""
    marker = "/repos/"
    path = urlsplit(repository_url).path
    if marker not in path:
        raise Malformed(f"a pull request named no repository: {printable(repository_url)}")
    name = path.split(marker, 1)[1].strip("/")
    if not name:
        raise Malformed(f"a pull request named no repository: {printable(repository_url)}")
    return name


def _author_of(result: IssueSearchResult) -> str:
    """The login of whoever opened it, or "" for a deleted account."""
    user = result.user
    if user is None:
        return ""
    login = user.login
    if not isinstance(login, str):
        return ""
    return printable(login)


def _pull_request_of(result: IssueSearchResult, category: Category) -> PullRequest:
    with _github_errors():
        repository = _repository_of(_text_of(result.repository_url, "repository_url"))
        number = result.number
        if not isinstance(number, int):
            raise Malformed(f"'number' was {type(number).__name__}, expected an integer")
        return PullRequest(
            # Unique across every repository in the tab, and stable across
            # refreshes — which is what the seen-state keys off.
            id=f"{repository}#{number}",
            updated_at=_moment_of(result.updated_at, "updated_at"),
            url=_text_of(result.html_url, "html_url"),
            number=number,
            title=printable(_text_of(result.title, "title")),
            repository=repository,
            author=_author_of(result),
            category=category,
        )


def fetch_detail(credentials: Credentials, http: httpx.Client, item: Item) -> PullRequestDetail:
    """The selected pull request's expanded view: description, branches, and
    the newest reviews (oldest first, so reading order matches the thread)."""
    if not isinstance(item, PullRequest):
        raise Malformed(f"expected a pull request, got {type(item).__name__}")
    with _client(credentials, lazy=True) as client, _github_errors():
        # The repository is addressed by name and nothing here reads its
        # payload, so a lazy client costs no request for it.
        repository = client.get_repo(item.repository)
        pull = repository.get_pull(item.number)
        raw_reviews = _first(pull.get_reviews(), REVIEWS_FETCH_LIMIT)
        reviews = sorted(
            (_review_of(raw) for raw in raw_reviews),
            # A pending review has no submission time; sorting it last keeps it
            # next to the newest submitted ones rather than at the top.
            key=lambda review: review.submitted_at or NEVER_SUBMITTED,
        )
        return PullRequestDetail(
            body=_body_of(pull),
            base=printable(pull.base.ref if pull.base else ""),
            head=printable(pull.head.ref if pull.head else ""),
            reviews=tuple(reviews[-REVIEW_LIMIT:]),
            hidden_reviews=max(0, len(raw_reviews) - REVIEW_LIMIT),
            hidden_is_lower_bound=len(raw_reviews) >= REVIEWS_FETCH_LIMIT,
        )


# Where a review that was never submitted sorts. Timezone-aware to match
# GitHub's own stamps: sorting an aware and a naive datetime together raises.
NEVER_SUBMITTED = datetime.max.replace(tzinfo=UTC)


def _body_of(pull: GithubPullRequest) -> str:
    """The description, sanitized whole and then capped — capping first could
    cut inside a markdown construct the panel then renders half of."""
    raw = pull.body
    if not isinstance(raw, str):
        return ""
    return capped(printable_block(raw, limit=None), BODY_LIMIT)


def _review_of(raw: PullRequestReview) -> Review:
    author = raw.user
    if author is None or not isinstance(author.login, str):
        name = ""
    else:
        name = printable(author.login)
    if isinstance(raw.state, str):
        state = printable(raw.state)
    else:
        state = ""
    if isinstance(raw.submitted_at, datetime):
        submitted_at = raw.submitted_at
    else:
        submitted_at = None
    return Review(author=name, state=state, submitted_at=submitted_at)
