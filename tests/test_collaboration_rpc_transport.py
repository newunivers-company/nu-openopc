from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from opc.layer4_tools.collaboration_rpc import (
    collaboration_rpc_transport_parent,
    start_collaboration_rpc_server,
)
from opc.layer4_tools.execution_context import wrap_command_for_context


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

    def test_invalid_workspace_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            blocked = Path(workspace) / "not-a-directory"
            blocked.write_text("blocked", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                "Cannot prepare collaboration RPC transport",
            ):
                collaboration_rpc_transport_parent(blocked)


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("bwrap"),
    "Linux bubblewrap is required for the real workspace-write boundary test",
)
class SandboxedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_round_trip_works_inside_workspace_write_sandbox(self) -> None:
        probe = subprocess.run(
            [
                shutil.which("bwrap") or "bwrap",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "/bin/sh",
                "-c",
                "true",
            ],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest("bubblewrap exists but user namespaces are unavailable")

        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp-test") as workspace:
            workspace_path = Path(workspace).resolve()
            parent = collaboration_rpc_transport_parent(workspace_path)

            async def _dispatch(
                tool_name: str,
                args: dict,
            ) -> tuple[dict, bool]:
                return {"tool_name": tool_name, "args": args}, False

            server = await start_collaboration_rpc_server(
                _dispatch,
                transport_parent=parent,
                transport="fifo",
            )
            assert server is not None
            try:
                script = "\n".join(
                    [
                        "import asyncio, json",
                        "from pathlib import Path",
                        "from opc.layer4_tools.collaboration_rpc import call_collaboration_rpc",
                        "Path('sandbox-write.txt').write_text('writable', encoding='utf-8')",
                        "result, is_error = asyncio.run(call_collaboration_rpc('inbox', {'limit': 3}))",
                        "print(json.dumps({'result': result, 'is_error': is_error}, sort_keys=True))",
                    ]
                )
                context = {
                    "workspace_root": str(workspace_path),
                    "sandbox": {
                        "platform": "linux",
                        "enabled": True,
                        "mode": "workspace-write",
                        "wrapper": "bwrap",
                        "fail_if_unavailable": True,
                        "allow_direct_fallback": False,
                        "allow_network": True,
                    },
                }
                command, metadata = wrap_command_for_context(
                    [sys.executable, "-c", script],
                    cwd=str(workspace_path),
                    context=context,
                )
                env = dict(os.environ)
                env.update(server.client_env)
                repo_root = str(Path(__file__).resolve().parents[1])
                env["PYTHONPATH"] = (
                    repo_root
                    + os.pathsep
                    + env.get("PYTHONPATH", "")
                )

                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(workspace_path),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()

                self.assertEqual(
                    process.returncode,
                    0,
                    stderr.decode("utf-8", errors="replace"),
                )
                self.assertEqual(metadata["effective_wrapper"], "bwrap")
                self.assertEqual(
                    (workspace_path / "sandbox-write.txt").read_text(
                        encoding="utf-8"
                    ),
                    "writable",
                )
                response = json.loads(stdout.decode("utf-8"))
                self.assertFalse(response["is_error"])
                self.assertEqual(response["result"]["tool_name"], "inbox")
                self.assertEqual(response["result"]["args"], {"limit": 3})
            finally:
                await server.close()


if __name__ == "__main__":
    unittest.main()
