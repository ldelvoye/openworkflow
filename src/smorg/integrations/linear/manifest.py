"""Linear's declaration; connects to Linear's MCP endpoint with OAuth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx

from smorg.auth.oauth import OAuthMethod
from smorg.auth.store import Credentials
from smorg.core.contract import Action, ActionClass, AuthPath, Item, Manifest
from smorg.integrations.linear.panel import LinearPanel
from smorg.integrations.linear.source import Issue, IssueDetail, fetch, fetch_detail

PROVIDER = OAuthMethod(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="smorg",
)

MANIFEST = Manifest(
    id="linear",
    display_name="Linear",
    connections=(AuthPath(id="mcp", method=PROVIDER),),
    stale_after=timedelta(minutes=5),
    actions=(Action(id="open", label="Open in Linear", key="o", action_class=ActionClass.LAUNCH),),
)


@dataclass(frozen=True)
class LinearIntegration:
    manifest: Manifest = MANIFEST
    panel_class: type[LinearPanel] = LinearPanel

    def fetch(self, credentials: Credentials, http: httpx.Client) -> tuple[Issue, ...]:
        return fetch(credentials, http)

    def fetch_detail(self, credentials: Credentials, http: httpx.Client, item: Item) -> IssueDetail:
        return fetch_detail(credentials, http, item)


INTEGRATION = LinearIntegration()
