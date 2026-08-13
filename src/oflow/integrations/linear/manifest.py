"""Linear's declaration.

Auth only for now: the source and panel arrive with the dashboard. Data comes
over the MCP endpoint rather than GraphQL because the workspace this targets
issues no personal API keys, and the MCP server offers dynamic client
registration — the token it returns is audience-bound to that endpoint, so
GraphQL is not a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from oflow.auth.oauth import ProviderConfig
from oflow.contract import Action, ActionClass, Manifest

PROVIDER = ProviderConfig(
    metadata_url="https://mcp.linear.app/.well-known/oauth-authorization-server",
    scopes=("read",),
    client_name="oflow",
)

MANIFEST = Manifest(
    id="linear",
    display_name="Linear",
    provider=PROVIDER,
    # Long enough that flicking between tabs does not refetch constantly, short
    # enough that a glance after stepping away is current.
    stale_after=timedelta(minutes=5),
    actions=(Action(id="open", label="Open in Linear", key="o", action_class=ActionClass.LAUNCH),),
)


@dataclass(frozen=True)
class LinearIntegration:
    manifest: Manifest = MANIFEST


INTEGRATION = LinearIntegration()
