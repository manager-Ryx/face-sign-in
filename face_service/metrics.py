"""Local, bounded measurement log (JSON Lines).

Each process owns its own file: the service records unlock attempts, camera
start-ups and learning events; the tray app records presence checks and
session events. Records contain only scalar measurements – never images,
face embeddings, or credentials – and the files rotate and expire so they
stay small.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_SCALARS = (str, int, float, bool, type(None))
_RESERVED = frozenset({"ts", "kind"})


def _clean(value: Any) -> Any:
    if isinstance(value, np.generic):  # numpy scalar, not an array
        value = value.item()
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 16:
        return [_clean(item) for item in value if isinstance(item, (*_SCALARS, np.generic))]
    raise TypeError(f"metrics accept scalars only, got {type(value).__name__}")


def _make_event(ts: float, kind: str, fields: dict[str, Any]) -> dict:
    event = {"ts": round(ts, 3), "kind": kind}
    for key, value in fields.items():
        if key in _RESERVED:
            raise ValueError(f"metrics field name {key!r} is reserved")
        try:
            event[key] = _clean(value)
        except TypeError:
            log.debug("metrics: dropped non-scalar field %s", key)
    return event


class MetricsStore:
    """Thread-safe, size-bounded JSONL event log with an in-memory tail."""

    def __init__(self, path: Path, *, max_bytes: int = 1_000_000, backups: int = 5,
                 retention_days: float = 90.0, memory_events: int = 10_000,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.retention_days = retention_days
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: deque[dict] = deque(maxlen=memory_events)
        self._loaded = False

    # ---- writing -----------------------------------------------------------

    def record(self, kind: str, /, **fields: Any) -> dict:
        event = _make_event(self._clock(), kind, fields)
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            self._ensure_loaded()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError as exc:
                log.warning("metrics write failed: %s", exc)
            self._recent.append(event)
        return event

    def _rotate(self) -> None:
        for index in range(self.backups, 0, -1):
            source = self._backup(index - 1) if index > 1 else self.path
            if source.exists():
                target = self._backup(index)
                if target.exists():
                    target.unlink()
                os.replace(source, target)
        self._expire()

    def _expire(self) -> None:
        cutoff = self._clock() - self.retention_days * 86400.0
        for index in range(1, self.backups + 1):
            backup = self._backup(index)
            try:
                if backup.exists() and backup.stat().st_mtime < cutoff:
                    backup.unlink()
            except OSError:
                pass

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    # ---- reading -----------------------------------------------------------

    def _files_oldest_first(self) -> list[Path]:
        files = [self._backup(i) for i in range(self.backups, 0, -1)]
        files.append(self.path)
        return [p for p in files if p.exists()]

    def _read_files(self) -> Iterable[dict]:
        for file in self._files_oldest_first():
            try:
                with file.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict) and "ts" in event and "kind" in event:
                            yield event
            except OSError:
                continue

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._recent.extend(self._read_files())
            self._loaded = True

    def events(self, kinds: Iterable[str] | None = None, since: float | None = None,
               *, from_disk: bool = False) -> list[dict]:
        """Return events (oldest first), optionally filtered by kind and time."""
        wanted = set(kinds) if kinds is not None else None
        if from_disk:
            source: Iterable[dict] = list(self._read_files())
        else:
            with self._lock:
                self._ensure_loaded()
                source = list(self._recent)
        cutoff = -math.inf if since is None else since
        return [e for e in source
                if e["ts"] >= cutoff and (wanted is None or e["kind"] in wanted)]

    def clear(self, kinds: Iterable[str] | None = None) -> int:
        """Delete events (all, or only the given kinds). Returns the count removed."""
        with self._lock:
            self._ensure_loaded()
            if kinds is None:
                removed = len(self._recent)
                self._recent.clear()
                for file in self._files_oldest_first():
                    file.unlink(missing_ok=True)
                return removed
            wanted = set(kinds)
            kept = [e for e in self._read_files() if e["kind"] not in wanted]
            removed = sum(1 for e in self._recent if e["kind"] in wanted)
            for file in self._files_oldest_first():
                file.unlink(missing_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                for event in kept:
                    fh.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
            self._recent.clear()
            self._recent.extend(kept)
            return removed


class NullMetrics(MetricsStore):
    """Metrics sink for tests and tools: records in memory only."""

    def __init__(self, clock: Callable[[], float] = time.time):
        super().__init__(Path(os.devnull), clock=clock)
        self._loaded = True

    def record(self, kind: str, /, **fields: Any) -> dict:
        event = _make_event(self._clock(), kind, fields)
        with self._lock:
            self._recent.append(event)
        return event

    def clear(self, kinds: Iterable[str] | None = None) -> int:
        with self._lock:
            if kinds is None:
                removed = len(self._recent)
                self._recent.clear()
                return removed
            wanted = set(kinds)
            kept = [e for e in self._recent if e["kind"] not in wanted]
            removed = len(self._recent) - len(kept)
            self._recent.clear()
            self._recent.extend(kept)
            return removed
