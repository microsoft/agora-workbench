"""Map state helpers — reading map_state.json with staleness tracking."""

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)
MAPS_DIR = Path(__file__).resolve().parents[1] / "maps"
MAP_STATE_FILE = MAPS_DIR / "map_state.json"

# Tracks the mtime of map_state.json at the time of last reset.
# Any map_state.json with mtime <= this value is treated as stale.
_map_state_invalidated_before: float = 0.0


def read_map_state() -> dict | None:
    """Read the current map_state.json, or None if stale/missing."""
    MAPS_DIR.mkdir(exist_ok=True)
    if MAP_STATE_FILE.is_file():
        if MAP_STATE_FILE.stat().st_mtime <= _map_state_invalidated_before:
            return None
        try:
            return json.loads(MAP_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            LOGGER.warning(f"Failed to read map_state.json: {e}")
    return None


def invalidate_map_state() -> None:
    """Mark the current map_state.json as stale and remove it from disk."""
    global _map_state_invalidated_before
    import time
    _map_state_invalidated_before = time.time()
    # Remove the file so a new session starts with a clean map.
    # The agent in the old session wrote it; the new session should not
    # inherit those layers.
    if MAP_STATE_FILE.is_file():
        try:
            MAP_STATE_FILE.unlink()
            LOGGER.info("Removed stale map_state.json on session reset")
        except PermissionError as e:
            LOGGER.warning(
                "Failed to remove map_state.json due to permissions; "
                "leaving file in place and relying on mtime-based invalidation: %s",
                e,
            )
        except OSError as e:
            LOGGER.warning("Failed to remove map_state.json: %s", e)
