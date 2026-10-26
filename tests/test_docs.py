"""The documentation stays in step with the code."""
from __future__ import annotations

import unittest

import _isolation  # noqa: F401

from tools.settings_table import current, render


class DocsTests(unittest.TestCase):
    def test_settings_table_matches_the_code(self) -> None:
        self.assertEqual(current(), render(), "run: python -m tools.settings_table --write")


if __name__ == "__main__":
    unittest.main()
