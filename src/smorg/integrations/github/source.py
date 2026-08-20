"""Fetch pull requests from GitHub through PyGithub and map them to typed items."""

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
from smorg.core.contract import (
    AccessNotAllowed,
    AuthExpired,
    IntegrationError,
    Item,
    Malformed,
    Unavailable,
)
from smorg.core.text import sanitize_block, sanitize_line, truncate


class Category(StrEnum):
    """Which of the dashboard's buckets a pull request landed in."""

    NEEDS_YOUR_REVIEW = "needs your review"
    NEEDS_TEAM_REVIEW = "needs your team's review"
    DRAFT = "drafts"
    WAITING = "waiting review or actions"
    NEEDS_ACTION = "needs actions"
    READY_TO_MERGE = "ready to merge"


REQUEST_TIMEOUT_SECONDS = 30
RESULTS_PER_PAGE = 50
MAX_PER_QUERY = 50
MAX_RETRIES = 2

REVIEW_LIMIT = 5
REVIEWS_FETCH_LIMIT = 25
BODY_LIMIT = 50_000

BASE_QUERY = "is:pr is:open archived:false"

# First match wins: the broad queries sit last so each takes only what the ones above left,
# which is how "your team's review" and "waiting" are computed.
QUERIES: tuple[tuple[Category, str], ...] = (
    (Category.NEEDS_YOUR_REVIEW, "user-review-requested:@me"),
    (Category.NEEDS_TEAM_REVIEW, "review-requested:@me"),
    (Category.DRAFT, "author:@me draft:true"),
    (Category.NEEDS_ACTION, "author:@me draft:false review:changes_requested"),
    (Category.NEEDS_ACTION, "author:@me draft:false status:failure"),
    (Category.READY_TO_MERGE, "author:@me draft:false review:approved status:success"),
    (Category.WAITING, "author:@me draft:false"),
)

# How GitHub names an organization that requires SSO, in the body of its 403.
SAML_ENFORCEMENT = "saml enforcement"

# Where a review that was never submitted sorts. Timezone-aware to match GitHub's own stamps:
# sorting an aware and a naive datetime together raises.
NEVER_SUBMITTED = datetime.max.replace(tzinfo=UTC)


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


def _message_of(error: GithubException) -> str:
    """The server's own explanation, when it sent a readable one."""
    data = error.data
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    return message if isinstance(message, str) else ""


def _translated(error: GithubException) -> IntegrationError:
    """The IntegrationError matching what would fix the failure: a new token (401), access
    granted (403), a corrected query (422), or waiting (any other status).
    """
    if error.status == 401:
        return AuthExpired("GitHub rejected the stored token; it may have expired or been revoked")
    if error.status == 403:
        if SAML_ENFORCEMENT in _message_of(error).casefold():
            return AccessNotAllowed("the token is not authorized for this organization's SSO")
        return AccessNotAllowed(
            "the token cannot reach this repository; check its organization access and scopes"
        )
    if error.status == 422:
        # GitHub refused a query this app wrote.
        return Malformed(f"GitHub refused the search: {sanitize_line(_message_of(error))}")
    return Unavailable(f"GitHub returned HTTP {error.status}")


@contextmanager
def _github_errors() -> Iterator[None]:
    """Turn everything PyGithub and its transport raise into IntegrationError."""
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

    `lazy` stops an object built from an address it was handed from fetching its own payload
    before anything reads it. That is how the detail pane addresses a repository by name without
    paying a request for it.
    """
    return Github(
        auth=Auth.Token(credentials.access_token),
        timeout=REQUEST_TIMEOUT_SECONDS,
        per_page=RESULTS_PER_PAGE,
        retry=MAX_RETRIES,
        lazy=lazy,
        # PyGithub paces every request by default, which GitHub asks for between writes. This
        # integration doesn't write, and writes keep their own separate pacing regardless.
        seconds_between_requests=0,
    )


def fetch(credentials: Credentials, http: httpx.Client) -> tuple[PullRequest, ...]:
    """Every open pull request that is yours or waiting on you, newest first.

    `http` goes unused: PyGithub brings its own transport, so the shell's shared httpx client
    has nothing to do here.
    """
    found: dict[str, PullRequest] = {}
    # Closed on the way out: a client owns a connection pool, and a dashboard that refreshes
    # every time you look at it would otherwise leave one behind per refresh.
    with _client(credentials) as client:
        for category, qualifiers in QUERIES:
            for result in _search(client, f"{BASE_QUERY} {qualifiers}"):
                pr = _pull_request_of(result, category)
                # setdefault, not assignment: QUERIES is in precedence order, so the first
                # category to claim a pull request keeps it.
                found.setdefault(pr.id, pr)
    newest_first = sorted(found.values(), key=lambda pr: pr.updated_at, reverse=True)
    return tuple(newest_first)


def _first[T: GithubObject](results: PaginatedList[T], limit: int) -> list[T]:
    """Up to `limit` results. A PaginatedList pages as it is walked, so stopping the walk is
    what stops the paging.
    """
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
    """The `owner/name` of a repository."""
    marker = "/repos/"
    path = urlsplit(repository_url).path
    if marker not in path:
        raise Malformed(f"a pull request named no repository: {sanitize_line(repository_url)}")
    name = path.split(marker, 1)[1].strip("/")
    if not name:
        raise Malformed(f"a pull request named no repository: {sanitize_line(repository_url)}")
    return name


def _author_of(result: IssueSearchResult) -> str:
    """The login of whoever opened the pull request, or "" for a deleted account."""
    user = result.user
    if user is None:
        return ""
    login = user.login
    if not isinstance(login, str):
        return ""
    return sanitize_line(login)


def _pull_request_of(result: IssueSearchResult, category: Category) -> PullRequest:
    with _github_errors():
        repository = _repository_of(_text_of(result.repository_url, "repository_url"))
        number = result.number
        if not isinstance(number, int):
            raise Malformed(f"'number' was {type(number).__name__}, expected an integer")
        return PullRequest(
            # Unique across every repository in the tab, and stable across refreshes — which
            # is what the seen-state keys off.
            id=f"{repository}#{number}",
            updated_at=_moment_of(result.updated_at, "updated_at"),
            url=_text_of(result.html_url, "html_url"),
            number=number,
            title=sanitize_line(_text_of(result.title, "title")),
            repository=repository,
            author=_author_of(result),
            category=category,
        )


def fetch_detail(credentials: Credentials, http: httpx.Client, item: Item) -> PullRequestDetail:
    """The selected pull request's expanded view: description, branches, and
    the newest reviews.
    """
    if not isinstance(item, PullRequest):
        raise Malformed(f"expected a pull request, got {type(item).__name__}")
    with _client(credentials, lazy=True) as client, _github_errors():
        repository = client.get_repo(item.repository)
        pr = repository.get_pull(item.number)
        raw_reviews = _first(pr.get_reviews(), REVIEWS_FETCH_LIMIT)
        all_reviews = [_review_of(raw) for raw in raw_reviews]
        oldest_first = sorted(all_reviews, key=_submitted_order)
        newest = oldest_first[-REVIEW_LIMIT:]
        return PullRequestDetail(
            body=_body_of(pr),
            base=sanitize_line(pr.base.ref if pr.base else ""),
            head=sanitize_line(pr.head.ref if pr.head else ""),
            reviews=tuple(newest),
            hidden_reviews=max(0, len(raw_reviews) - REVIEW_LIMIT),
            hidden_is_lower_bound=len(raw_reviews) >= REVIEWS_FETCH_LIMIT,
        )


def _submitted_order(review: Review) -> datetime:
    """Pending reviews sort last."""
    if review.submitted_at is None:
        return NEVER_SUBMITTED
    return review.submitted_at


def _body_of(pr: GithubPullRequest) -> str:
    """The description, sanitized and capped, or "" if there is none."""
    raw = pr.body
    if not isinstance(raw, str):
        return ""
    return truncate(sanitize_block(raw, limit=None), BODY_LIMIT)


def _review_of(raw: PullRequestReview) -> Review:
    """A typed Review; anything GitHub omitted degrades to "" or None."""
    author = raw.user
    if author is None or not isinstance(author.login, str):
        name = ""
    else:
        name = sanitize_line(author.login)
    if isinstance(raw.state, str):
        state = sanitize_line(raw.state)
    else:
        state = ""
    if isinstance(raw.submitted_at, datetime):
        submitted_at = raw.submitted_at
    else:
        submitted_at = None
    return Review(author=name, state=state, submitted_at=submitted_at)
