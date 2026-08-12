from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opc.core.models import Task
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.operations.campaign_runner import (
    RESULT_SKELETON_NAME,
    SubprocessExecutorConfig,
    SubprocessSlotExecutor,
    collect_workspace_artifacts,
    refresh_workspace_artifacts,
)


class WorkspaceArtifactSafetyTests(unittest.TestCase):
    def test_snapshot_is_text_only_bounded_and_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "deliverable.py").write_bytes(
                b"print('verified')\r\n"
            )
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (root / "private.pem").write_text("secret\n", encoding="utf-8")
            (root / "binary.bin").write_bytes(b"\x00\x01")
            (root / ".opc").mkdir()
            (root / ".opc" / "runtime.json").write_text("{}", encoding="utf-8")

            artifacts, report = collect_workspace_artifacts(root)

            self.assertEqual(
                artifacts,
                {"workspace/src/deliverable.py": "print('verified')\n"},
            )
            self.assertEqual(report["captured_files"], 1)
            skipped = {item["path"]: item["reason"] for item in report["skipped"]}
            self.assertEqual(skipped[".env"], "secret_name")
            self.assertEqual(skipped["private.pem"], "secret_name")
            self.assertEqual(skipped["binary.bin"], "binary")

    def test_refresh_rebinds_digest_and_evidence_to_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workplace"
            artifact_dir = root / "artifacts"
            workspace.mkdir()
            artifact_dir.mkdir()
            (workspace / "solution.py").write_text("VALUE = 1\n", encoding="utf-8")
            (artifact_dir / "output.md").write_text("# Delivery\n", encoding="utf-8")
            (artifact_dir / RESULT_SKELETON_NAME).write_text(
                json.dumps(
                    {
                        "artifact_digest": "old",
                        "evidence": {"correctness": ["output.md"]},
                        "metadata": {"executor": {}},
                    }
                ),
                encoding="utf-8",
            )

            report = refresh_workspace_artifacts(artifact_dir, workspace)
            skeleton = json.loads(
                (artifact_dir / RESULT_SKELETON_NAME).read_text(encoding="utf-8")
            )

            self.assertEqual(skeleton["artifact_digest"], report["artifact_digest"])
            self.assertIn(
                "workspace/solution.py", skeleton["evidence"]["correctness"]
            )
            self.assertNotIn(
                RESULT_SKELETON_NAME, skeleton["evidence"]["correctness"]
            )
            self.assertEqual(
                skeleton["metadata"]["executor"]["workspace_snapshot"][
                    "captured_files"
                ],
                1,
            )


class AbsoluteSlotDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_kills_the_process_group(self) -> None:
        executor = SubprocessSlotExecutor(
            project_id="deadline-test",
            config=SubprocessExecutorConfig(
                timeout_seconds=0.1,
                termination_grace_seconds=0.1,
            ),
        )
        loop = asyncio.get_running_loop()
        executor._slot_started_monotonic = loop.time()
        executor._slot_deadline_monotonic = loop.time() + 0.1
        started = time.monotonic()

        result = await executor._invoke(
            [sys.executable, "-c", "import time; time.sleep(10)"]
        )

        self.assertTrue(result["timed_out"])
        self.assertTrue(result["deadline_exceeded"])
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_continuation_cannot_reset_the_slot_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "opc.operations.campaign_runner.get_project_workplace",
            return_value=Path(tmpdir),
        ):
            executor = SubprocessSlotExecutor(
                project_id="deadline-test",
                config=SubprocessExecutorConfig(timeout_seconds=10),
            )
            spawned: list[list[str]] = []

            async def fake_spawn(command: list[str]) -> dict[str, object]:
                spawned.append(command)
                executor._slot_deadline_monotonic = (
                    asyncio.get_running_loop().time()
                )
                return {
                    "command": command,
                    "timed_out": False,
                    "exit_code": 0,
                    "stdout": json.dumps(
                        {
                            "ok": True,
                            "task_id": "task-1",
                            "task_status": "waiting",
                            "response": "Dispatched and confirmed; currently running; "
                            "runtime will reactivate.",
                        }
                    ),
                    "stderr_tail": "",
                }

            executor._spawn = fake_spawn  # type: ignore[method-assign]
            result = await executor(
                {"mode": "company", "prompt": "Build"}, Path(tmpdir)
            )

            self.assertFalse(result.success)
            self.assertTrue(result.metadata["deadline_exceeded"])
            self.assertEqual(len(spawned), 1)
            self.assertEqual(result.metadata["process_invocations"], 1)


class ExternalAgentCallBudgetTests(unittest.TestCase):
    def test_permits_fail_closed_at_the_exact_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"OPC_BENCHMARK_EXTERNAL_CALL_LIMIT": "2"},
        ):
            task = Task(id="budget-task", title="Budget", project_id="benchmark")
            reservations = [
                ExternalAgentBroker._reserve_benchmark_external_call(
                    workspace_path=tmpdir,
                    task=task,
                    agent_type="codex",
                )
                for _ in range(3)
            ]

            self.assertEqual(
                [item["allowed"] for item in reservations],
                [True, True, False],
            )
            self.assertEqual(reservations[-1]["reason"], "exhausted")
            permits = list(
                (
                    Path(tmpdir)
                    / ".opc"
                    / "benchmark_external_call_budget"
                ).glob("call-*.json")
            )
            self.assertEqual(len(permits), 2)
