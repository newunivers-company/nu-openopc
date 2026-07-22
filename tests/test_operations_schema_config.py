from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
import unittest

from opc.core.config import OPCConfig
from opc.database.store import OPCStore


class OperationsSchemaAndConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_initialization_adds_versioned_operations_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = OPCStore(Path(raw_root) / "tasks.db")
            await store.initialize()
            try:
                async with store._require_db().execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ) as cursor:
                    tables = {str(row[0]) for row in await cursor.fetchall()}
                expected = {
                    "operations_schema",
                    "goal_contracts",
                    "goal_contract_versions",
                    "run_manifests",
                    "run_scorecards",
                    "operating_events",
                    "outbox_messages",
                    "run_leases",
                    "learning_assets",
                    "learning_asset_evaluations",
                    "capability_attempts",
                    "staffing_decisions",
                }
                self.assertTrue(expected.issubset(tables))
                async with store._require_db().execute(
                    "SELECT version FROM operations_schema WHERE component = 'operating_kernel'"
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(row[0], 1)
            finally:
                await store.close()

    async def test_reopening_existing_database_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "tasks.db"
            first = OPCStore(path)
            await first.initialize()
            await first.close()
            second = OPCStore(path)
            await second.initialize()
            try:
                async with second._require_db().execute(
                    "SELECT COUNT(*) FROM operations_schema"
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(row[0], 1)
            finally:
                await second.close()


class OperationsConfigTests(unittest.TestCase):
    def test_operations_config_round_trips_through_system_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            config_dir = Path(raw_root)
            config = OPCConfig()
            config.system.operations.evaluation.minimum_total_score = 0.88
            config.system.operations.durable.lease_seconds = 45
            config.system.operations.learning.minimum_sample_size = 7
            config.system.operations.staffing.quality_weight = 0.5
            config.save(config_dir)

            loaded = OPCConfig.load(config_dir)
            self.assertEqual(loaded.system.operations.evaluation.minimum_total_score, 0.88)
            self.assertEqual(loaded.system.operations.durable.lease_seconds, 45)
            self.assertEqual(loaded.system.operations.learning.minimum_sample_size, 7)
            self.assertEqual(loaded.system.operations.staffing.quality_weight, 0.5)

    def test_legacy_config_without_operations_uses_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            config_dir = Path(raw_root)
            (config_dir / "system_config.yaml").write_text(
                "system:\n  log_level: INFO\nautonomy: {}\ncapabilities: {}\n",
                encoding="utf-8",
            )
            config = OPCConfig.load(config_dir)
            self.assertTrue(config.system.operations.enabled)
            self.assertEqual(config.system.operations.durable.outbox_max_attempts, 5)
            self.assertEqual(config.system.operations.evaluation.minimum_total_score, 0.75)

    def test_checked_in_config_contains_complete_operations_policy(self) -> None:
        config = OPCConfig.load(Path("config"))
        self.assertTrue(config.system.operations.enabled)
        self.assertEqual(
            set(config.system.operations.evaluation.normalized_weights()),
            {"quality", "evidence", "budget", "reliability", "autonomy"},
        )
        self.assertEqual(
            set(config.system.operations.staffing.normalized_weights()),
            {"quality", "domain", "reliability", "experience", "availability", "cost"},
        )

    def test_ci_regression_script_fails_for_regressed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            baseline = json.loads(
                Path("tests/fixtures/operations_scorecard_baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            candidate = dict(baseline)
            candidate["run_id"] = "regressed"
            candidate["scorecard_id"] = "regressed"
            candidate["quality_score"] = 0.7
            candidate_path = root / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/operations_regression_gate.py",
                    "--baseline",
                    "tests/fixtures/operations_scorecard_baseline.json",
                    "--candidate",
                    str(candidate_path),
                    "--maximum-regression",
                    "0.05",
                ],
                cwd=Path.cwd(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("quality_score regressed", result.stdout)


if __name__ == "__main__":
    unittest.main()
