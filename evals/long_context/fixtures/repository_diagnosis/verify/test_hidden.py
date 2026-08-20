from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from model_identity import model_matches  # noqa: E402


class ModelIdentityTests(unittest.TestCase):
    def test_exact_and_implicit_latest_only(self) -> None:
        self.assertTrue(model_matches("qwen3.8", "qwen3.8"))
        self.assertTrue(model_matches("qwen3.8", "qwen3.8:latest"))
        self.assertTrue(model_matches("qwen3.8:q6", "qwen3.8:q6"))
        self.assertFalse(model_matches("qwen3.8", "qwen3.8:q4"))
        self.assertFalse(model_matches("qwen3.8:q6", "qwen3.8:latest"))


if __name__ == "__main__":
    unittest.main()
