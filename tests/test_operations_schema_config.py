from __future__ import annotations

import json
import sqlite3
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
                    "outbox_delivery_receipts",
                    "run_leases",
                    "learning_assets",
                    "learning_asset_evaluations",
                    "capability_attempts",
                    "route_execution_contracts",
                    "provider_usage_events",
                    "provider_canary_results",
                    "resource_approval_uses",
                    "provider_call_reservations",
                    "staffing_decisions",
                    "operator_actions",
                }
                self.assertTrue(expected.issubset(tables))
                async with store._require_db().execute(
                    "SELECT version FROM operations_schema WHERE component = 'operating_kernel'"
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(row[0], 4)
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

    async def test_v1_fixture_migrates_to_v4_and_backfills_goal_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "tasks.db"
            fixture = Path("tests/fixtures/operations_schema_v1.sql").read_text(
                encoding="utf-8"
            )
            with sqlite3.connect(path) as connection:
                connection.executescript(fixture)
            store = OPCStore(path)
            await store.initialize()
            try:
                async with store._require_db().execute(
                    "SELECT version FROM operations_schema WHERE component = 'operating_kernel'"
                ) as cursor:
                    version = await cursor.fetchone()
                async with store._require_db().execute(
                    "SELECT COUNT(*) FROM goal_contract_versions WHERE goal_id = 'legacy-goal'"
                ) as cursor:
                    history = await cursor.fetchone()
                self.assertEqual(version[0], 4)
                self.assertEqual(history[0], 1)
            finally:
                await store.close()

    async def test_newer_schema_fails_closed_instead_of_downgrading(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "tasks.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """CREATE TABLE operations_schema (
                           component TEXT PRIMARY KEY,
                           version INTEGER NOT NULL,
                           updated_at TEXT NOT NULL
                       );
                       INSERT INTO operations_schema VALUES (
                           'operating_kernel', 999, '2026-01-01T00:00:00+00:00'
                       );"""
                )
            store = OPCStore(path)
            try:
                with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                    await store.initialize()
            finally:
                await store.close()


class OperationsConfigTests(unittest.TestCase):
    def test_operations_config_round_trips_through_system_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            config_dir = Path(raw_root)
            config = OPCConfig()
            config.system.operations.evaluation.minimum_total_score = 0.88
            config.system.operations.durable.lease_seconds = 45
            config.system.operations.learning.minimum_sample_size = 7
            config.system.operations.staffing.quality_weight = 0.5
            config.system.operations.providers.subscription_call_limit = 25
            config.save(config_dir)

            loaded = OPCConfig.load(config_dir)
            self.assertEqual(loaded.system.operations.evaluation.minimum_total_score, 0.88)
            self.assertEqual(loaded.system.operations.durable.lease_seconds, 45)
            self.assertEqual(loaded.system.operations.learning.minimum_sample_size, 7)
            self.assertEqual(loaded.system.operations.staffing.quality_weight, 0.5)
            self.assertEqual(
                loaded.system.operations.providers.subscription_call_limit, 25
            )

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
            self.assertEqual(
                config.system.operations.providers.subscription_call_limit, 200
            )

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
        self.assertTrue(config.system.operations.providers.status_canary_enabled)
        self.assertEqual(
            config.system.operations.providers.subscription_call_limit, 200
        )
        self.assertEqual(
            config.system.operations.providers.readiness_min_observation_seconds,
            86_400,
        )
        self.assertEqual(
            config.system.operations.providers.readiness_max_sample_age_seconds,
            1_800,
        )
        self.assertEqual(
            config.system.operations.providers.readiness_max_gap_seconds,
            28_800,
        )
        self.assertEqual(
            config.system.operations.providers.readiness_required_failure_scenarios,
            [
                "credential_expiry",
                "transport_timeout",
                "quota_exhaustion",
                "model_drift",
            ],
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
