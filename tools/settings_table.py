"""Render the "Every setting, classified" table in docs/ADAPTATION.md from the code.

    python -m tools.settings_table          # print the table
    python -m tools.settings_table --write  # update the document in place
"""
from __future__ import annotations

import sys
from dataclasses import MISSING, fields
from pathlib import Path

from face_service.config import Config, Kind, settings_specs

DOC = Path(__file__).resolve().parent.parent / "docs" / "ADAPTATION.md"
START = "Generated from the code (`face_service/config.py`).\n\n"
WHO = {Kind.PREFERENCE: "You decide", Kind.AUTOMATIC: "Measured, automatic within bounds",
       Kind.SAFEGUARD: "Safeguard (never changes silently)"}


def _number(value: float) -> str:
    return f"{value:g}"


def render() -> str:
    specs = settings_specs()
    rows = ["| Setting | Who controls it | Default | Bounds | What it does |",
            "|---|---|---|---|---|"]
    for f in fields(Config):
        spec = specs[f.name]
        default = "from Windows" if f.default is MISSING else str(f.default)
        if spec.choices:
            bounds = ", ".join(spec.choices)
        elif spec.minimum is not None and spec.maximum is not None:
            bounds = f"{_number(spec.minimum)}-{_number(spec.maximum)}"
        else:
            bounds = ""
        rows.append(f"| `{f.name}` | {WHO[spec.kind]} | `{default}` | {bounds} | "
                    f"{spec.summary} |")
    return "\n".join(rows) + "\n"


def current() -> str:
    """The table as it appears in the document now."""
    text = DOC.read_text(encoding="utf-8")
    table = text.split(START, 1)[1]
    lines = []
    for line in table.splitlines(keepends=True):
        if not line.startswith("|"):
            break
        lines.append(line)
    return "".join(lines)


def write() -> None:
    text = DOC.read_text(encoding="utf-8")
    DOC.write_text(text.replace(START + current(), START + render(), 1), encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        sys.stdout.write(render())
