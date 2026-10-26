"""Source files stay clean UTF-8: no byte-order marks or double-encoded text."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("face_service", "presence_monitor", "tools", "tests")
MOJIBAKE = ("â€", "Â·", "Ã©", "â†")


class SourceHygieneTests(unittest.TestCase):
    def test_python_sources_are_clean_utf8(self) -> None:
        problems = []
        for folder in FOLDERS:
            for path in (ROOT / folder).rglob("*.py"):
                data = path.read_bytes()
                if data.startswith(b"\xef\xbb\xbf"):
                    problems.append(f"{path.relative_to(ROOT)}: byte-order mark")
                text = data.decode("utf-8")
                for marker in MOJIBAKE:
                    if marker in text and path.name != Path(__file__).name:
                        problems.append(f"{path.relative_to(ROOT)}: garbled text {marker!r}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
