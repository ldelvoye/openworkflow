"""Small, integration-agnostic text formatting a panel's rendering can reuse.

Nothing here talks to a service or knows about a specific integration's data
shape — just formatting decisions any panel might want.
"""

from __future__ import annotations

from datetime import datetime

from smorg.auth.store import now


def age(moment: datetime) -> str:
    """How long ago `moment` was, as a short "5m" / "3h" / "2d" label."""
    delta = now() - moment
    # A future stamp is clock skew, and anything under a minute reads the
    # same either way.
    if delta.total_seconds() < 60:
        return "now"
    if delta.days >= 1:
        return f"{delta.days}d"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return f"{delta.seconds // 60}m"
