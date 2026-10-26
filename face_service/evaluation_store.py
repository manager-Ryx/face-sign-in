"""Held-out genuine face samples, used only to *evaluate* recognition.

These embeddings are never matched against during unlock and never added to
the profile. Their distances to the current profile show how reliably you
are recognised as the profile changes. Sources: recognition tests you run,
and one spare frame from confirmed face sign-ins (marked as possibly
optimistic, since only successful sign-ins contribute). Bounded and local.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .evaluation import GenuineSample
from .profile import FaceProfile, unit

log = logging.getLogger(__name__)

LIMIT = 600


class EvaluationStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._rows: np.ndarray | None = None
        self._meta: dict[str, np.ndarray] = {}
        self._load()

    def _load(self) -> None:
        try:
            with np.load(self.path, allow_pickle=False) as data:
                self._rows = np.asarray(data["embeddings"], dtype=np.float32)
                self._meta = {k: np.asarray(data[k]) for k in ("ts", "source", "lighting",
                                                               "session", "order")}
        except FileNotFoundError:
            self._rows = None
        except (OSError, KeyError, ValueError) as exc:
            log.warning("evaluation samples unreadable, starting fresh: %s", exc)
            self._rows = None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix="evaluation-", suffix=".npz")
        with os.fdopen(fd, "wb") as handle:
            np.savez(handle, embeddings=self._rows, **self._meta)
        os.replace(name, self.path)

    def __len__(self) -> int:
        return 0 if self._rows is None else len(self._rows)

    def add(self, embeddings: list[np.ndarray], *, source: str, lighting: list[str],
            session: str, ts: float | None = None) -> None:
        if not embeddings:
            return
        ts = time.time() if ts is None else ts
        rows = np.stack([unit(e) for e in embeddings]).astype(np.float32)
        meta = {"ts": np.full(len(rows), ts, dtype=np.float64),
                "source": np.asarray([source] * len(rows), dtype="<U8"),
                "lighting": np.asarray(lighting, dtype="<U8"),
                "session": np.asarray([session] * len(rows), dtype="<U24"),
                "order": np.arange(len(rows), dtype=np.int16)}
        with self._lock:
            if self._rows is None:
                self._rows, self._meta = rows, meta
            else:
                self._rows = np.vstack((self._rows, rows))
                self._meta = {k: np.concatenate((self._meta[k], meta[k])) for k in meta}
            if len(self._rows) > LIMIT:
                self._rows = self._rows[-LIMIT:]
                self._meta = {k: v[-LIMIT:] for k, v in self._meta.items()}
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._rows, self._meta = None, {}
            self.path.unlink(missing_ok=True)

    def samples(self, profile: FaceProfile) -> list[GenuineSample]:
        with self._lock:
            if self._rows is None or profile.size == 0:
                return []
            distances = (1.0 - self._rows @ profile.unit_rows.T).min(axis=1)
            return [GenuineSample(float(d), time.strftime("%Y-%m-%d", time.localtime(float(t))),
                                  str(light), str(src))
                    for d, t, light, src in zip(distances, self._meta["ts"],
                                                self._meta["lighting"], self._meta["source"], strict=True)]

    def sequences(self, profile: FaceProfile) -> list[list[float]]:
        """Distance sequences of recorded multi-frame checks, in capture order."""
        with self._lock:
            if self._rows is None or profile.size == 0:
                return []
            distances = (1.0 - self._rows @ profile.unit_rows.T).min(axis=1)
            grouped: dict[str, list[tuple[int, float]]] = {}
            for d, src, session, order in zip(distances, self._meta["source"],
                                              self._meta["session"], self._meta["order"], strict=True):
                if str(src) == "check":
                    grouped.setdefault(str(session), []).append((int(order), float(d)))
            return [[d for _, d in sorted(items)] for items in grouped.values()
                    if len(items) >= 3]
