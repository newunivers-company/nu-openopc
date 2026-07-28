from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.layer4_tools.collaboration_rpc import collaboration_rpc_transport_parent


class TransportParentTests(unittest.TestCase):
    """The RPC FIFO must live inside the role workspace: external-agent
    sandboxes (Codex workspace-write) reject /tmp writes with EROFS, which
    made every collaboration call fail in the 2026-07-29 pilot."""

    def test_parent_is_rooted_in_workspace_comms(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            parent = collaboration_rpc_transport_parent(workspace)
            self.assertIsNotNone(parent)
            self.assertTrue(parent.is_dir())
            self.assertEqual(
                parent, Path(workspace).resolve() / ".opc-comms" / "rpc"
            )

    def test_missing_workspace_falls_back_to_tempdir_behavior(self) -> None:
        self.assertIsNone(collaboration_rpc_transport_parent(""))
        self.assertIsNone(collaboration_rpc_transport_parent(None))


if __name__ == "__main__":
    unittest.main()
