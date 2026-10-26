"""Crash-safe reading and writing of the small state files in the data folder."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` so readers see the old or the new content, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=1))


def read_state(path: Path, empty: dict[str, Any], *, what: str) -> dict[str, Any]:
    """Load a versioned JSON state file into ``empty``.

    Only keys present in ``empty`` are taken. A missing file, a damaged file,
    or a different ``version`` leaves ``empty`` as it is: state files hold
    learned estimates, and starting afresh is always safe.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as exc:
        log.warning("%s unreadable, starting fresh: %s", what, exc)
        return empty
    if not isinstance(data, dict) or data.get("version") != empty.get("version"):
        log.warning("%s has an unknown format, starting fresh", what)
        return empty
    empty.update({key: value for key, value in data.items() if key in empty})
    return empty
