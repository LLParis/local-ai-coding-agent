from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from agent_continuity.checkpoint import CheckpointError, _relative_path  # noqa: E402


class CanonicalCrossPlatformPathTest(unittest.TestCase):
    def test_safe_windows_relative_path_is_canonical(self) -> None:
        self.assertEqual(_relative_path(r"src\package\module.py"), "src/package/module.py")
        self.assertEqual(_relative_path("./src/package/module.py"), "src/package/module.py")

    def test_ambiguous_or_escaping_windows_forms_are_rejected(self) -> None:
        values = (
            r"C:\outside.py",
            r"C:outside.py",
            r"\outside.py",
            r"\\server\share\outside.py",
            r"src\..\outside.py",
            r"src/..\outside.py",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(CheckpointError):
                _relative_path(value)


if __name__ == "__main__":
    unittest.main()
