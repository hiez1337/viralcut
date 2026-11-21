"""Custom auto-update defaults for the personal fork."""
from __future__ import annotations

import os
from typing import Any, Dict

DEFAULT_UPDATE_CHECK = False


def resolve_update_enabled(state: Dict[str, Any]) -> bool:
    """Resolve whether upstream update checks are enabled (default False on this fork)."""
    raw = os.environ.get("VIRALCUT_UPDATE_CHECK")
    if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    if isinstance(state.get("enabled"), bool):
        return bool(state["enabled"])
    if raw is not None and raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return DEFAULT_UPDATE_CHECK
