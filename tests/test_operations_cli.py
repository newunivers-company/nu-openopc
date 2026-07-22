from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from opc.cli.app import app


class OperationsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.opc_home = self.root / ".opc"
        self.patch = patch("opc.cli.operations.get_opc_home", return_value=self.opc_home)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self._tmp.cleanup()

    def _invoke(self, args: list[str], *, expected_exit: int = 0):
        result = self.runner.invoke(app, args)
        self.assertEqual(result.exit_code, expected_exit, result.output)
        return result

    def _write_json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_goal_run_score_gate_and_mission_loop(self) -> None:
        contract = self._write_json(
            "contract.json",
            {
                "goal_id": "goal-cli",
                "project_id": "demo",
                "title": "CLI operating loop",
                "objective": "Prove the CLI closes the evaluation loop.",
                "acceptance_criteria": [
                    {
                        "criterion_id": "quality",
                        "description": "Output quality remains acceptable",
                        "minimum_score": 0.8,
                        "evidence_required": True,
                    }
                ],
                "budget": {"max_cost_usd": 1.0},
            },
        )
        goal = self._invoke(
            ["ops", "goal", "create", "--contract", str(contract), "--project", "demo"]
        )
        self.assertEqual(json.loads(goal.output)["goal_id"], "goal-cli")

        baseline_input = self._write_json(
            "baseline.json",
            {
                "criterion_scores": {"quality": 0.95},
                "evidence": {"quality": ["artifact://baseline"]},
                "metrics": {"cost_usd": 0.2, "total_attempts": 1},
                "baseline_label": "main",
            },
        )
        current_input = self._write_json(
            "current.json",
            {
                "criterion_scores": {"quality": 0.85},
                "evidence": {"quality": ["artifact://current"]},
                "metrics": {"cost_usd": 0.2, "total_attempts": 1},
            },
        )
        for run_id, result_path in (
            ("run-baseline", baseline_input),
            ("run-current", current_input),
        ):
            self._invoke(
                [
                    "ops",
                    "run",
                    "start",
                    "goal-cli",
                    "--run-id",
                    run_id,
                    "--project",
                    "demo",
                ]
            )
            self._invoke(
                ["ops", "run", "finish", run_id, "--project", "demo"]
            )
            scored = self._invoke(
                [
                    "ops",
                    "evaluate",
                    "score",
                    run_id,
                    "--result",
                    str(result_path),
                    "--project",
                    "demo",
                ]
            )
            self.assertEqual(json.loads(scored.output)["gate_status"], "pass")

        regression = self._invoke(
            [
                "ops",
                "evaluate",
                "gate",
                "run-current",
                "--baseline-run",
                "run-baseline",
                "--project",
                "demo",
            ],
            expected_exit=1,
        )
        regression_payload = json.loads(regression.output)
        self.assertFalse(regression_payload["passed"])
        self.assertGreater(regression_payload["regressions"]["quality_score"], 0.05)

        status = self._invoke(
            ["ops", "mission", "status", "--project", "demo"]
        )
        status_payload = json.loads(status.output)
        self.assertEqual(status_payload["project_id"], "demo")
        self.assertEqual(status_payload["active_goals"], 1)
        self.assertEqual(status_payload["active_runs"], 0)
        self.assertEqual(status_payload["pending_outbox"], 2)

        brief = self._invoke(["ops", "mission", "brief", "--project", "demo"])
        self.assertIn("Mission Control — demo", brief.output)

    def test_learning_and_staffing_json_commands(self) -> None:
        candidate = self._write_json(
            "candidate.json",
            {
                "name": "cli-policy",
                "kind": "policy",
                "content": {"rule": "verify evidence"},
                "source_run_ids": ["run-1"],
                "confidence": 0.9,
            },
        )
        created = self._invoke(
            [
                "ops",
                "learning",
                "create",
                "--candidate",
                str(candidate),
                "--project",
                "demo",
            ]
        )
        asset_id = json.loads(created.output)["asset_id"]
        evaluated = self._invoke(
            [
                "ops",
                "learning",
                "evaluate",
                asset_id,
                "--phase",
                "offline",
                "--score",
                "0.9",
                "--sample-size",
                "3",
                "--evidence",
                "artifact://eval",
                "--project",
                "demo",
            ]
        )
        self.assertTrue(json.loads(evaluated.output)["passed"])

        staffing = self._write_json(
            "staffing.json",
            {
                "role_id": "qa",
                "required_domains": ["testing"],
                "candidates": [
                    {
                        "employee_id": "employee-b",
                        "role_ids": ["qa"],
                        "domains": ["testing"],
                        "quality_score": 0.7,
                    },
                    {
                        "employee_id": "employee-a",
                        "role_ids": ["qa"],
                        "domains": ["testing"],
                        "quality_score": 0.9,
                    },
                ],
            },
        )
        recommended = self._invoke(
            [
                "ops",
                "staffing",
                "recommend",
                "--request",
                str(staffing),
                "--project",
                "demo",
            ]
        )
        self.assertEqual(
            json.loads(recommended.output)["selected_employee_id"],
            "employee-a",
        )

    def test_invalid_project_id_is_rejected(self) -> None:
        result = self.runner.invoke(
            app,
            ["ops", "mission", "status", "--project", "../escape"],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.root / "escape").exists())

    def test_goal_close_creates_audited_terminal_version(self) -> None:
        created = self._invoke(
            [
                "ops",
                "goal",
                "create",
                "--title",
                "Closable goal",
                "--objective",
                "Close with an operator reason",
                "--criterion",
                "done=All work is complete",
                "--project",
                "demo",
            ]
        )
        goal_id = json.loads(created.output)["goal_id"]

        closed = self._invoke(
            [
                "ops",
                "goal",
                "close",
                goal_id,
                "--status",
                "cancelled",
                "--reason",
                "superseded",
                "--project",
                "demo",
            ]
        )
        payload = json.loads(closed.output)

        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["metadata"]["closure"]["reason"], "superseded")

    def test_capability_request_cannot_cross_project_boundary(self) -> None:
        request = self._write_json(
            "cross-project.json",
            {
                "capability_kind": "llm",
                "task_type": "dialogue",
                "project_id": "another-project",
            },
        )
        result = self.runner.invoke(
            app,
            [
                "ops",
                "capability",
                "plan",
                "--request",
                str(request),
                "--project",
                "demo",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not match CLI project", str(result.exception))


if __name__ == "__main__":
    unittest.main()
