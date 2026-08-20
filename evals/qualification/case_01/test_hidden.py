from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from agent_continuity.launcher import primary_plan  # noqa: E402


class ModelReadinessIdentityTest(unittest.TestCase):
    @mock.patch("agent_continuity.launcher._codex_binary", return_value="codex")
    @mock.patch("agent_continuity.launcher._validate_profile")
    @mock.patch("agent_continuity.launcher.validate_checkpoint", return_value={"digest": "ok"})
    @mock.patch("agent_continuity.launcher.ensure_tunnel")
    def test_probe_uses_the_exact_profile_model(
        self,
        ensure_tunnel: mock.Mock,
        _checkpoint: mock.Mock,
        _profile: mock.Mock,
        _binary: mock.Mock,
    ) -> None:
        ensure_tunnel.return_value = SimpleNamespace(
            action="reused",
            endpoint=SimpleNamespace(model="gpt-oss-20b"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = primary_plan(
                workspace=root,
                checkpoint_path=root / "checkpoint.json",
                task="Continue.",
            )
        self.assertEqual(ensure_tunnel.call_args.kwargs["model"], "gpt-oss-20b")
        self.assertEqual(plan.endpoint.model, "gpt-oss-20b")


if __name__ == "__main__":
    unittest.main()
