"""Lookup over the integration allowlist."""

from __future__ import annotations

from oflow.contract import Integration


class UnknownIntegration(Exception):
    """No integration by that id is registered in this build."""


def _by_id() -> dict[str, Integration]:
    # Imported per call rather than at module load so the allowlist stays a
    # single mutable source of truth, readable by tests without reloading.
    from oflow import integrations

    return {entry.manifest.id: entry for entry in integrations.INTEGRATIONS}


def known_integration_ids() -> tuple[str, ...]:
    return tuple[str, ...](sorted(_by_id()))


def get_integration(integration_id: str) -> Integration:
    registry = _by_id()
    if integration_id in registry:
        return registry[integration_id]
    if not registry:
        raise UnknownIntegration(
            f"{integration_id!r} is not supported: no integrations are registered in this build"
        )
    raise UnknownIntegration(
        f"{integration_id!r} is not a supported integration. "
        f"Available: {', '.join(sorted(registry))}"
    )
