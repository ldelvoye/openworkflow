"""GitHub's declaration; connects with a personal access token."""

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
    connections=(ConnectionPath(id="token", method=TOKEN),),
    # Keep above 5min to avoid rate limiting
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
