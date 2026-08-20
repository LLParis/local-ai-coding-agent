from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from agent_continuity import tunnel  # noqa: E402


class TunnelControlMasterOwnershipTest(unittest.TestCase):
    def test_unproven_existing_listener_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            fake_fcntl = mock.Mock(LOCK_EX=2)
            with (
                mock.patch.object(tunnel.sys, "platform", "darwin"),
                mock.patch.object(tunnel, "fcntl", fake_fcntl),
                mock.patch.object(tunnel, "_runtime_dir", return_value=runtime),
                mock.patch.object(
                    tunnel,
                    "listener_names",
                    return_value=["127.0.0.1:12434"],
                ),
                mock.patch.object(
                    tunnel,
                    "_run",
                    return_value=subprocess.CompletedProcess([], 1, "", "no master"),
                ) as run,
                mock.patch.object(tunnel, "check_endpoint") as endpoint,
            ):
                with self.assertRaises(tunnel.TunnelError):
                    tunnel.ensure_tunnel(tunnel.TunnelSpec())
            self.assertTrue(run.called)
            endpoint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
