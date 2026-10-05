"""Run manifest — per-school pipeline state, persisted after every update.

States: FETCHED | QUARANTINED | FETCH_FAILED | EXTRACTED | EVALUATED | SCORED
A school may legitimately regress (e.g. --force refetch); the manifest keeps
the latest state plus attempt history counts, never silently drops a failure.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


class RunManifest:
    def __init__(self, path: str | Path = "data/run_manifest.json"):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Never lose a corrupt manifest silently — keep it aside.
                corrupt = self.path.with_suffix(".corrupt.json")
                self.path.replace(corrupt)
                self.entries = {}

    async def update(self, school_id: str, state: str, **fields):
        async with self._lock:
            e = self.entries.setdefault(str(school_id), {"attempts": 0})
            e["state"] = state
            e["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if state in ("FETCHED", "QUARANTINED", "FETCH_FAILED"):
                e["attempts"] = e.get("attempts", 0) + 1
            e.update(fields)
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.entries.values():
            counts[e.get("state", "?")] = counts.get(e.get("state", "?"), 0) + 1
        return counts
