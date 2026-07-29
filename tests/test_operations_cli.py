from __future__ import annotations

import json
import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from opc.cli.app import app
from opc.core.models import DelegationWorkItem
from opc.database.store import OPCStore
from opc.layer2_organization.phase import Phase


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

    def test_capability_slo_cli_enforces_explicit_sample_floor(self) -> None:
        request = self._write_json(
            "canary.json",
            {
                "capability_kind": "llm",
                "task_type": "dialogue",
                "project_id": "demo",
                "allow_live": False,
            },
        )
        for _ in range(2):
            self._invoke([
                "ops",
                "capability",
                "canary",
                "--request",
                str(request),
                "--project",
                "demo",
            ])

        result = self._invoke([
            "ops",
            "capability",
            "slo",
            "--provider",
            "openopc_config",
            "--minimum-samples",
            "3",
            "--trend-window-samples",
            "2",
            "--project",
            "demo",
        ])

        slo = json.loads(result.output)["providers"]["openopc_config"]
        self.assertEqual(slo["samples"], 2)
        self.assertEqual(slo["sample_target"], 3)
        self.assertEqual(slo["attainment_state"], "insufficient_samples")
        self.assertFalse(slo["target_met"])

    def test_capability_readiness_writes_auditable_blocked_report(self) -> None:
        request = self._write_json(
            "readiness-canary.json",
            {
                "capability_kind": "llm",
                "task_type": "dialogue",
                "project_id": "demo",
                "allow_live": False,
            },
        )
        self._invoke([
            "ops",
            "capability",
            "canary",
            "--request",
            str(request),
            "--project",
            "demo",
        ])
        output = self.root / "readiness.json"

        result = self._invoke(
            [
                "ops",
                "capability",
                "readiness",
                "--provider",
                "openopc_config",
                "--minimum-samples",
                "2",
                "--minimum-observation-seconds",
                "86400",
                "--minimum-time-buckets",
                "4",
                "--required-drill",
                "credential_expiry",
                "--output",
                str(output),
                "--fail-on-blocked",
                "--project",
                "demo",
            ],
            expected_exit=1,
        )

        report = json.loads(result.output)
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
        readiness = report["providers"]["openopc_config"]
        self.assertFalse(readiness["production_ready"])
        self.assertFalse(readiness["sample_target_met"])
        self.assertFalse(readiness["observation_target_met"])
        self.assertEqual(
            readiness["missing_failure_scenarios"],
            ["credential_expiry"],
        )


if __name__ == "__main__":
    unittest.main()


class BenchmarkRunSlotDryRunTests(unittest.TestCase):
    """A-3 smoke: plan -> run-slot --dry-run works without any OPC home."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plan_and_dry_run_roundtrip(self) -> None:
        plan_path = self.root / "plan.json"
        planned = self.runner.invoke(
            app,
            [
                "ops", "benchmark", "plan",
                "--campaign-id", "ci-smoke",
                "--output", str(plan_path),
            ],
        )
        self.assertEqual(planned.exit_code, 0, planned.output)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        slot_id = plan["slots"][0]["slot_id"]

        result = self.runner.invoke(
            app,
            [
                "ops", "benchmark", "run-slot", slot_id,
                "--plan", str(plan_path),
                "--dry-run",
                "-p", "demo",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["run_id"], plan["slots"][0]["run_id"])
        self.assertIn("opc", payload["command"][0])
        self.assertIn("--json", payload["command"])

    def test_tampered_plan_is_rejected_before_any_execution(self) -> None:
        plan_path = self.root / "plan.json"
        self.runner.invoke(
            app,
            [
                "ops", "benchmark", "plan",
                "--campaign-id", "ci-smoke",
                "--output", str(plan_path),
            ],
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["slots"][0]["prompt"] = "tampered"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        result = self.runner.invoke(
            app,
            [
                "ops", "benchmark", "run-slot", plan["slots"][0]["slot_id"],
                "--plan", str(plan_path),
                "--dry-run",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_campaign_status_does_not_sweep_live_work_item_claims(self) -> None:
        opc_home = self.root / ".opc"
        project = "campaign-live"
        plan_path = self.root / "plan.json"
        planned = self.runner.invoke(
            app,
            [
                "ops", "benchmark", "plan",
                "--campaign-id", "claim-safe",
                "--output", str(plan_path),
            ],
        )
        self.assertEqual(planned.exit_code, 0, planned.output)
        db_path = opc_home / "projects" / project / "tasks.db"

        async def seed() -> None:
            store = OPCStore(db_path)
            await store.initialize()
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="work-live",
                    run_id="run-live",
                    cell_id="cell-live",
                    role_id="engineer",
                    seat_id="seat::engineer",
                    title="Long-running external work",
                    phase=Phase.RUNNING,
                    claimed_by_role_runtime_session_id="role-session-live",
                    claimed_by_seat_id="seat::engineer",
                )
            )
            await store.close()

        asyncio.run(seed())
        with patch("opc.cli.operations.get_opc_home", return_value=opc_home):
            result = self.runner.invoke(
                app,
                [
                    "ops", "benchmark", "campaign-status",
                    "--plan", str(plan_path),
                    "--project", project,
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)

        async def inspect() -> DelegationWorkItem | None:
            store = OPCStore(db_path)
            await store.initialize(run_startup_maintenance=False)
            item = await store.get_delegation_work_item("work-live")
            await store.close()
            return item

        item = asyncio.run(inspect())
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(
            item.claimed_by_role_runtime_session_id,
            "role-session-live",
        )
        self.assertEqual(item.claimed_by_seat_id, "seat::engineer")
