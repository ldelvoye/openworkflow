"""Lookup over the integration allowlist."""

from __future__ import annotations

from smorg.core.contract import Integration, Manifest


class UnknownIntegration(Exception):
    """No integration by that id is registered in this build."""


def _by_id() -> dict[str, Integration]:
    # Imported per call, not at module load, so tests can swap the allowlist
    # without reloading this module.
    from smorg import integrations

    registry: dict[str, Integration] = {}
    for entry in integrations.INTEGRATIONS:
        identifier = entry.manifest.id
        if identifier in registry:
            raise ValueError(f"two registered integrations share id {identifier!r}")
        registry[identifier] = entry
    return registry


def known_integration_ids() -> tuple[str, ...]:
    registry = _by_id()
    return tuple[str, ...](sorted(registry))


def manifests() -> tuple[Manifest, ...]:
    """Every registered manifest, sorted by id — the enumeration surface a
    tab-management menu lists from."""
    registry = _by_id()
    return tuple[Manifest, ...](registry[identifier].manifest for identifier in sorted(registry))


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
