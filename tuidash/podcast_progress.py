from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_PATH = Path.home() / ".local" / "share" / "tuidash" / "podcast_progress.json"
_COMPLETE_THRESHOLD = 0.80   # fraction of duration that counts as "completed"
_START_THRESHOLD    = 5.0    # seconds before an episode is considered "started"
_NEW_WINDOW         = 7 * 86400  # seconds — episodes newer than this are "new" if untouched


@dataclass
class EpisodeProgress:
    status: str        # "started" | "completed"
    position: float    # seconds
    duration: float    # seconds
    last_updated: str  # ISO-8601 UTC


class ProgressStore:
    """Thread-safe local JSON store for podcast episode progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    # ── public API ────────────────────────────────────────────────────────────

    def get_status(self, episode_id: int, date_published: int) -> str:
        """Return 'new', 'started', 'completed', or '' (old and unplayed)."""
        with self._lock:
            entry = self._data.get(str(episode_id))
        if entry:
            return entry["status"]
        age = time.time() - date_published
        return "new" if (date_published > 0 and age < _NEW_WINDOW) else ""

    def get_position(self, episode_id: int) -> float:
        """Saved playback position in seconds (0 if not started or completed)."""
        with self._lock:
            entry = self._data.get(str(episode_id), {})
        if entry.get("status") == "completed":
            return 0.0   # replay completed episodes from the beginning
        return float(entry.get("position", 0.0))

    def update(self, episode_id: int, position: float, duration: float) -> str:
        """Record playback progress; returns the resulting status string."""
        key = str(episode_id)
        with self._lock:
            entry = self._data.get(key, {})
            current = entry.get("status", "")

            if current == "completed":
                return "completed"   # completed is sticky

            if duration > 0 and position / duration >= _COMPLETE_THRESHOLD:
                new_status = "completed"
            elif position >= _START_THRESHOLD:
                new_status = "started"
            else:
                return current

            self._data[key] = {
                "status":       new_status,
                "position":     round(position, 1),
                "duration":     round(duration, 1),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

        self._save_unlocked()
        return new_status

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            self._data = json.loads(_PATH.read_text())
        except Exception:
            self._data = {}

    def _save_unlocked(self) -> None:
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(json.dumps(self._data, indent=2))
        except Exception:
            pass


# Module-level singleton — shared across all widget instances.
store = ProgressStore()
