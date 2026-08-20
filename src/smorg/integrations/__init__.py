"""Every integration this build supports.

The allowlist. Adding an integration means adding its package here; anything
absent is not connectable, so an unsupported service fails with "not supported"
rather than half-working.
"""

from __future__ import annotations

from smorg.core.contract import Integration
from smorg.integrations import github, linear

INTEGRATIONS: tuple[Integration, ...] = (github.INTEGRATION, linear.INTEGRATION)
