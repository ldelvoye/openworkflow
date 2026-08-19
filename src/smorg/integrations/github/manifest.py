"""GitHub's declaration.

Connects with a personal access token rather than OAuth. GitHub publishes no
OAuth metadata document and accepts no dynamic client registration, so an OAuth
connection would need an app somebody registered by hand and a client id shipped
or configured before the first login — a setup step per user, to reach the same
place a token they paste once already reaches.

Nothing renews a token, so a token that expires or is revoked surfaces as
AuthExpired on the next fetch, and the tab says to run `smorg connect github`
again. That is the re-authentication path; there is no other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx

from smorg.auth.store import Credentials
from smorg.auth.token import TokenPrompt
from smorg.core.contract import Action, ActionClass, ConnectionPath, Item, Manifest
from smorg.integrations.github.panel import GitHubPanel
from smorg.integrations.github.source import (
    PullRequest,
    PullRequestDetail,
    fetch,
    fetch_detail,
)

# A classic token needs `repo` to see private repositories' pull requests at
# all — GitHub's classic scopes have no read-only equivalent — and `read:org`
# for the team-review queries. A fine-grained token is the tighter choice and
# the one named first, since its read-only permissions match what this tab
# actually does.
TOKEN = TokenPrompt(
    label="GitHub personal access token",
    help_url="https://github.com/settings/personal-access-tokens",
    scopes_hint=(
        "read access to Pull requests and Metadata (fine-grained), "
        "or the repo and read:org scopes (classic)"
    ),
)

MANIFEST = Manifest(
    id="github",
    display_name="GitHub",
    connections=(ConnectionPath(id="token", token=TOKEN),),
    # One refresh costs seven searches against a limit of thirty a minute, so
    # this is the floor a flick between tabs must not go under, not just a
    # freshness preference.
    stale_after=timedelta(minutes=5),
    actions=(Action(id="open", label="Open in GitHub", key="o", action_class=ActionClass.LAUNCH),),
)


@dataclass(frozen=True)
class GitHubIntegration:
    manifest: Manifest = MANIFEST
    panel_class: type[GitHubPanel] = GitHubPanel

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[PullRequest, ...]:
        return fetch(credentials, http)

    def fetch_detail(
        self, credentials: Credentials, http: httpx.Client, item: Item
    ) -> PullRequestDetail:
        return fetch_detail(credentials, http, item)


INTEGRATION = GitHubIntegration()
