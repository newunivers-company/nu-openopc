"""Persistence and migrations for the OpenOPC operating kernel."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Mapping, TYPE_CHECKING
import uuid

from opc.operations.models import (
    CapabilityAttempt,
    GateStatus,
    GoalContract,
    GoalContractStatus,
    LearningAsset,
    LearningAssetEvaluation,
    OutboxMessage,
    OutboxDeliveryReceipt,
    OperatorAction,
    ProviderUsageEvent,
    ProviderCanaryResult,
    RouteExecutionContract,
    RunManifest,
    RunScorecard,
    RunStatus,
    StaffingDecision,
    utc_now,
)

if TYPE_CHECKING:
    from opc.database.store import OPCStore, _SQLiteConnectionAdapter


OPERATIONS_SCHEMA_VERSION = 4


class ProviderCallQuotaExceeded(PermissionError):
    """Raised when an atomic subscription call reservation cannot be made."""


async def create_operations_schema(db: "_SQLiteConnectionAdapter") -> None:
    """Create the additive operations schema on an initialized project DB."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS operations_schema (
               component TEXT PRIMARY KEY,
               version INTEGER NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    async with db.execute(
        "SELECT version FROM operations_schema WHERE component = 'operating_kernel'"
    ) as cursor:
        existing = await cursor.fetchone()
    if existing is not None and int(existing[0]) > OPERATIONS_SCHEMA_VERSION:
        raise RuntimeError(
            f"operations schema {int(existing[0])} is newer than supported "
            f"schema {OPERATIONS_SCHEMA_VERSION}; upgrade OpenOPC before opening this database"
        )
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS operations_schema (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_contracts (
            goal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            organization_id TEXT DEFAULT '',
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_contract_versions (
            goal_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            organization_id TEXT DEFAULT '',
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(goal_id, version)
        );

        CREATE TABLE IF NOT EXISTS run_manifests (
            run_id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            organization_id TEXT DEFAULT '',
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_scorecards (
            scorecard_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            goal_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            gate_status TEXT NOT NULL,
            total_score REAL NOT NULL,
            baseline_label TEXT DEFAULT '',
            payload TEXT NOT NULL,
            evaluated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operating_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            idempotency_key TEXT,
            payload TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE(aggregate_type, aggregate_id, aggregate_version),
            UNIQUE(idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS outbox_messages (
            message_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at TEXT NOT NULL,
            lease_owner TEXT DEFAULT '',
            lease_token INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT,
            last_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            delivered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS outbox_delivery_receipts (
            message_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY(message_id, consumer_id)
        );

        CREATE TABLE IF NOT EXISTS run_leases (
            run_id TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS learning_assets (
            asset_id TEXT PRIMARY KEY,
            organization_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            employee_id TEXT DEFAULT '',
            role_id TEXT DEFAULT '',
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            previous_asset_id TEXT DEFAULT '',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(organization_id, project_id, name, version)
        );

        CREATE TABLE IF NOT EXISTS learning_asset_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            score REAL NOT NULL,
            passed INTEGER NOT NULL,
            payload TEXT NOT NULL,
            evaluated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS capability_attempts (
            attempt_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            run_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            capability_kind TEXT NOT NULL,
            provider TEXT DEFAULT '',
            candidate_id TEXT DEFAULT '',
            status TEXT NOT NULL,
            cost_usd REAL,
            latency_ms REAL NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS route_execution_contracts (
            contract_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            route_id TEXT NOT NULL UNIQUE,
            run_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            capability_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            planned_provider TEXT DEFAULT '',
            actual_provider TEXT DEFAULT '',
            planned_model TEXT DEFAULT '',
            actual_model TEXT DEFAULT '',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS provider_usage_events (
            usage_event_id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            run_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            capability_kind TEXT NOT NULL,
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            measured INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            total_tokens INTEGER,
            cost_usd REAL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_canary_results (
            canary_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            capability_kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            candidate_id TEXT DEFAULT '',
            model TEXT DEFAULT '',
            mode TEXT NOT NULL,
            success INTEGER NOT NULL,
            latency_ms REAL NOT NULL,
            model_drift INTEGER NOT NULL DEFAULT 0,
            error_category TEXT DEFAULT '',
            payload TEXT NOT NULL,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_approval_uses (
            token_digest TEXT PRIMARY KEY,
            approval_id TEXT DEFAULT '',
            operator_id TEXT DEFAULT '',
            key_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            claims TEXT NOT NULL,
            consumed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_call_reservations (
            reservation_id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT DEFAULT '',
            status TEXT NOT NULL,
            window_seconds INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS staffing_decisions (
            decision_id TEXT PRIMARY KEY,
            run_id TEXT DEFAULT '',
            project_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            selected_employee_id TEXT NOT NULL,
            predicted_score REAL NOT NULL,
            observed_score REAL,
            regret REAL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operator_actions (
            action_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            operator_id TEXT DEFAULT '',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            executed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_goal_contracts_project_status
            ON goal_contracts(project_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_goal_contract_versions_project
            ON goal_contract_versions(project_id, goal_id, version);
        CREATE INDEX IF NOT EXISTS idx_run_manifests_goal_status
            ON run_manifests(goal_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_run_manifests_project_status
            ON run_manifests(project_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_run_scorecards_project_gate
            ON run_scorecards(project_id, gate_status, evaluated_at);
        CREATE INDEX IF NOT EXISTS idx_operating_events_run_sequence
            ON operating_events(run_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_outbox_claim
            ON outbox_messages(status, next_attempt_at, lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_outbox_receipts_consumer_completed
            ON outbox_delivery_receipts(consumer_id, completed_at);
        CREATE INDEX IF NOT EXISTS idx_learning_assets_project_status
            ON learning_assets(project_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_learning_asset_eval_asset_phase
            ON learning_asset_evaluations(asset_id, phase, evaluated_at);
        CREATE INDEX IF NOT EXISTS idx_capability_attempts_run_created
            ON capability_attempts(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_route_contracts_project_status
            ON route_execution_contracts(project_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_route_contracts_run_created
            ON route_execution_contracts(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_project_created
            ON provider_usage_events(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_provider_created
            ON provider_usage_events(provider, created_at);
        CREATE INDEX IF NOT EXISTS idx_provider_canary_project_checked
            ON provider_canary_results(project_id, checked_at);
        CREATE INDEX IF NOT EXISTS idx_provider_canary_provider_checked
            ON provider_canary_results(provider, checked_at);
        CREATE INDEX IF NOT EXISTS idx_resource_approval_project_consumed
            ON resource_approval_uses(project_id, consumed_at);
        CREATE INDEX IF NOT EXISTS idx_provider_call_quota_window
            ON provider_call_reservations(project_id, provider, created_at, status);
        CREATE INDEX IF NOT EXISTS idx_staffing_decisions_run_role
            ON staffing_decisions(run_id, role_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_operator_actions_project_status
            ON operator_actions(project_id, status, created_at);
        """
    )
    await _ensure_columns(
        db,
        "resource_approval_uses",
        {
            "approval_id": "TEXT DEFAULT ''",
            "operator_id": "TEXT DEFAULT ''",
            "key_id": "TEXT DEFAULT ''",
        },
    )
    await db.execute(
        """INSERT OR IGNORE INTO goal_contract_versions
           (goal_id, version, project_id, organization_id, payload, recorded_at)
           SELECT goal_id, version, project_id, organization_id, payload, updated_at
           FROM goal_contracts"""
    )
    await db.execute(
        """INSERT INTO operations_schema(component, version, updated_at)
           VALUES ('operating_kernel', ?, ?)
           ON CONFLICT(component) DO UPDATE SET
               version = excluded.version,
               updated_at = excluded.updated_at""",
        (OPERATIONS_SCHEMA_VERSION, utc_now().isoformat()),
    )


async def _ensure_columns(
    db: "_SQLiteConnectionAdapter",
    table: str,
    columns: Mapping[str, str],
) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {str(row[1]) for row in await cursor.fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class OperationsRepository:
    """Typed access to operations tables in one project-scoped ``OPCStore``."""

    def __init__(self, store: "OPCStore") -> None:
        self.store = store
        lock = getattr(store, "_operations_transaction_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(store, "_operations_transaction_lock", lock)
        self.transaction_lock: asyncio.Lock = lock

    @property
    def db(self) -> "_SQLiteConnectionAdapter":
        return self.store._require_db()

    async def ensure_ready(self) -> None:
        await self.store.ensure_ready()

    def rebind(self, store: "OPCStore") -> None:
        self.__init__(store)

    def assert_project(self, project_id: str) -> None:
        """Reject access that does not match the bound project database."""
        store_project = str(self.store.project_id or "").strip()
        entity_project = str(project_id or "").strip()
        if store_project and entity_project and store_project != entity_project:
            raise RuntimeError(
                "operations write rejected cross-project data: "
                f"store_project={store_project!r} entity_project={entity_project!r}"
            )

    def _assert_project(self, project_id: str) -> None:
        self.assert_project(project_id)

    async def save_goal(self, goal: GoalContract) -> GoalContract:
        goal.validate()
        self._assert_project(goal.project_id)
        async with self.transaction_lock:
            async with self.db.execute("BEGIN IMMEDIATE") as cursor:
                if cursor is None:
                    raise RuntimeError("could not begin goal contract transaction")
            try:
                existing = await self.get_goal(goal.goal_id)
                if existing is None:
                    if goal.version != 1:
                        raise ValueError("a new goal contract must start at version 1")
                else:
                    if (
                        existing.project_id != goal.project_id
                        or existing.organization_id != goal.organization_id
                    ):
                        raise ValueError("goal contract identity cannot change across versions")
                    if goal.version != existing.version + 1:
                        raise ValueError(
                            f"goal contract update must use version {existing.version + 1}; "
                            f"received {goal.version}"
                        )
                    goal.created_at = existing.created_at
                goal.updated_at = utc_now()
                payload = _dump(goal.to_dict())
                await self.db.execute(
                    """INSERT INTO goal_contract_versions
                       (goal_id, version, project_id, organization_id, payload, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        goal.goal_id,
                        goal.version,
                        goal.project_id,
                        goal.organization_id,
                        payload,
                        goal.updated_at.isoformat(),
                    ),
                )
                await self.db.execute(
                    """INSERT INTO goal_contracts
                       (goal_id, project_id, organization_id, title, status, version,
                        payload, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(goal_id) DO UPDATE SET
                           project_id = excluded.project_id,
                           organization_id = excluded.organization_id,
                           title = excluded.title,
                           status = excluded.status,
                           version = excluded.version,
                           payload = excluded.payload,
                           updated_at = excluded.updated_at""",
                    (
                        goal.goal_id,
                        goal.project_id,
                        goal.organization_id,
                        goal.title,
                        goal.status.value,
                        goal.version,
                        payload,
                        goal.created_at.isoformat(),
                        goal.updated_at.isoformat(),
                    ),
                )
                await self.db.commit()
                return goal
            except Exception:
                await self.db.rollback()
                raise

    async def get_goal(self, goal_id: str) -> GoalContract | None:
        payload = await self._payload_one(
            "SELECT payload FROM goal_contracts WHERE goal_id = ?",
            (goal_id,),
        )
        return GoalContract.from_dict(payload) if payload else None

    async def get_goal_version(
        self,
        goal_id: str,
        version: int,
    ) -> GoalContract | None:
        payload = await self._payload_one(
            """SELECT payload FROM goal_contract_versions
               WHERE goal_id = ? AND version = ?""",
            (goal_id, int(version)),
        )
        return GoalContract.from_dict(payload) if payload else None

    async def list_goals(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[GoalContract]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT payload FROM goal_contracts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [GoalContract.from_dict(item) for item in await self._payload_all(query, params)]

    async def save_manifest(
        self,
        manifest: RunManifest,
        *,
        commit: bool = True,
    ) -> RunManifest:
        manifest.validate()
        self._assert_project(manifest.project_id)
        latest_goal = await self.get_goal(manifest.goal_id)
        if latest_goal is None:
            raise KeyError(f"goal contract not found: {manifest.goal_id}")
        if manifest.goal_version == 0:
            manifest.goal_version = latest_goal.version
            goal = latest_goal
        else:
            goal = await self.get_goal_version(manifest.goal_id, manifest.goal_version)
            if goal is None:
                raise KeyError(
                    f"goal contract version not found: "
                    f"{manifest.goal_id}@{manifest.goal_version}"
                )
        if goal.project_id != manifest.project_id:
            raise ValueError("run manifest project_id must match its goal contract")
        manifest.updated_at = utc_now()
        await self.db.execute(
            """INSERT INTO run_manifests
               (run_id, goal_id, project_id, organization_id, status, payload,
                started_at, completed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   goal_id = excluded.goal_id,
                   project_id = excluded.project_id,
                   organization_id = excluded.organization_id,
                   status = excluded.status,
                   payload = excluded.payload,
                   started_at = excluded.started_at,
                   completed_at = excluded.completed_at,
                   updated_at = excluded.updated_at""",
            (
                manifest.run_id,
                manifest.goal_id,
                manifest.project_id,
                manifest.organization_id,
                manifest.status.value,
                _dump(manifest.to_dict()),
                _iso(manifest.started_at),
                _iso(manifest.completed_at),
                manifest.created_at.isoformat(),
                manifest.updated_at.isoformat(),
            ),
        )
        if commit:
            await self.db.commit()
        return manifest

    async def get_manifest(self, run_id: str) -> RunManifest | None:
        payload = await self._payload_one(
            "SELECT payload FROM run_manifests WHERE run_id = ?",
            (run_id,),
        )
        return RunManifest.from_dict(payload) if payload else None

    async def list_manifests(
        self,
        *,
        project_id: str | None = None,
        goal_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[RunManifest]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if goal_id:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        clean_statuses = [str(item).strip() for item in statuses or [] if str(item).strip()]
        if clean_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        query = "SELECT payload FROM run_manifests"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [RunManifest.from_dict(item) for item in await self._payload_all(query, params)]

    async def save_scorecard(self, scorecard: RunScorecard) -> RunScorecard:
        self._assert_project(scorecard.project_id)
        manifest = await self.get_manifest(scorecard.run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {scorecard.run_id}")
        if manifest.goal_id != scorecard.goal_id or manifest.project_id != scorecard.project_id:
            raise ValueError("scorecard identity must match its run manifest")
        await self.db.execute(
            """INSERT INTO run_scorecards
               (scorecard_id, run_id, goal_id, project_id, gate_status, total_score,
                baseline_label, payload, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   scorecard_id = excluded.scorecard_id,
                   goal_id = excluded.goal_id,
                   project_id = excluded.project_id,
                   gate_status = excluded.gate_status,
                   total_score = excluded.total_score,
                   baseline_label = excluded.baseline_label,
                   payload = excluded.payload,
                   evaluated_at = excluded.evaluated_at""",
            (
                scorecard.scorecard_id,
                scorecard.run_id,
                scorecard.goal_id,
                scorecard.project_id,
                scorecard.gate_status.value,
                scorecard.total_score,
                scorecard.baseline_label,
                _dump(scorecard.to_dict()),
                scorecard.evaluated_at.isoformat(),
            ),
        )
        await self.db.commit()
        return scorecard

    async def save_scorecard_and_settle_goal(
        self,
        scorecard: RunScorecard,
        *,
        evaluated_goal: GoalContract,
        manifest: RunManifest,
        completion_requested: bool,
    ) -> tuple[RunScorecard, bool]:
        """Persist a scorecard and its optional goal closure in one transaction."""

        self._assert_project(scorecard.project_id)
        if (
            scorecard.run_id != manifest.run_id
            or scorecard.goal_id != manifest.goal_id
            or scorecard.project_id != manifest.project_id
        ):
            raise ValueError("scorecard identity must match its run manifest")
        if evaluated_goal.goal_id != manifest.goal_id:
            raise ValueError("evaluated goal must match its run manifest")

        async with self.transaction_lock:
            async with self.db.execute("BEGIN IMMEDIATE") as cursor:
                if cursor is None:
                    raise RuntimeError("could not begin scorecard settlement transaction")
            try:
                latest_payload = await self._payload_one(
                    "SELECT payload FROM goal_contracts WHERE goal_id = ?",
                    (evaluated_goal.goal_id,),
                )
                latest = GoalContract.from_dict(latest_payload) if latest_payload else None
                completed = False
                if (
                    completion_requested
                    and scorecard.gate_status == GateStatus.PASS
                    and latest is not None
                    and latest.status == GoalContractStatus.ACTIVE
                    and latest.version == manifest.goal_version
                ):
                    active_statuses = (
                        RunStatus.PENDING.value,
                        RunStatus.RUNNING.value,
                        RunStatus.BLOCKED.value,
                    )
                    async with self.db.execute(
                        """SELECT COUNT(*) FROM run_manifests
                           WHERE goal_id = ? AND status IN (?, ?, ?)""",
                        (latest.goal_id, *active_statuses),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if int(row[0] if row else 0) == 0:
                        terminal = GoalContract.from_dict(latest.to_dict())
                        terminal.version = latest.version + 1
                        terminal.status = GoalContractStatus.COMPLETED
                        terminal.updated_at = utc_now()
                        terminal.metadata = {
                            **dict(latest.metadata),
                            "completion": {
                                "source": "passing_run_scorecard",
                                "run_id": manifest.run_id,
                                "scorecard_id": scorecard.scorecard_id,
                                "score": scorecard.total_score,
                            },
                        }
                        terminal_payload = _dump(terminal.to_dict())
                        await self.db.execute(
                            """INSERT INTO goal_contract_versions
                               (goal_id, version, project_id, organization_id, payload, recorded_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                terminal.goal_id,
                                terminal.version,
                                terminal.project_id,
                                terminal.organization_id,
                                terminal_payload,
                                terminal.updated_at.isoformat(),
                            ),
                        )
                        await self.db.execute(
                            """UPDATE goal_contracts SET status = ?, version = ?, payload = ?,
                               updated_at = ? WHERE goal_id = ? AND version = ?""",
                            (
                                terminal.status.value,
                                terminal.version,
                                terminal_payload,
                                terminal.updated_at.isoformat(),
                                terminal.goal_id,
                                latest.version,
                            ),
                        )
                        scorecard.metadata["goal_auto_completed"] = True
                        completed = True

                await self.db.execute(
                    """INSERT INTO run_scorecards
                       (scorecard_id, run_id, goal_id, project_id, gate_status, total_score,
                        baseline_label, payload, evaluated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                           scorecard_id = excluded.scorecard_id,
                           goal_id = excluded.goal_id,
                           project_id = excluded.project_id,
                           gate_status = excluded.gate_status,
                           total_score = excluded.total_score,
                           baseline_label = excluded.baseline_label,
                           payload = excluded.payload,
                           evaluated_at = excluded.evaluated_at""",
                    (
                        scorecard.scorecard_id,
                        scorecard.run_id,
                        scorecard.goal_id,
                        scorecard.project_id,
                        scorecard.gate_status.value,
                        scorecard.total_score,
                        scorecard.baseline_label,
                        _dump(scorecard.to_dict()),
                        scorecard.evaluated_at.isoformat(),
                    ),
                )
                await self.db.commit()
                return scorecard, completed
            except Exception:
                await self.db.rollback()
                raise

    async def get_scorecard(self, run_id: str) -> RunScorecard | None:
        payload = await self._payload_one(
            "SELECT payload FROM run_scorecards WHERE run_id = ?",
            (run_id,),
        )
        return RunScorecard.from_dict(payload) if payload else None

    async def list_scorecards(
        self,
        *,
        project_id: str | None = None,
        goal_id: str | None = None,
        gate_status: str | None = None,
        baseline_label: str | None = None,
        limit: int = 100,
    ) -> list[RunScorecard]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("goal_id", goal_id),
            ("gate_status", gate_status),
            ("baseline_label", baseline_label),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT payload FROM run_scorecards"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY evaluated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [RunScorecard.from_dict(item) for item in await self._payload_all(query, params)]

    async def list_events(self, run_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT sequence, event_id, run_id, aggregate_type, aggregate_id,
                      aggregate_version, event_type, idempotency_key, payload, occurred_at
               FROM operating_events WHERE run_id = ? ORDER BY sequence ASC LIMIT ?""",
            (run_id, max(1, min(int(limit), 10_000))),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "sequence": int(row[0]),
                "event_id": row[1],
                "run_id": row[2],
                "aggregate_type": row[3],
                "aggregate_id": row[4],
                "aggregate_version": int(row[5]),
                "event_type": row[6],
                "idempotency_key": row[7] or "",
                "payload": _load(row[8]),
                "occurred_at": row[9],
            }
            for row in rows
        ]

    async def get_event_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        async with self.db.execute(
            """SELECT sequence, event_id, run_id, aggregate_type, aggregate_id,
                      aggregate_version, event_type, idempotency_key, payload, occurred_at
               FROM operating_events WHERE idempotency_key = ?""",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "sequence": int(row[0]),
            "event_id": row[1],
            "run_id": row[2],
            "aggregate_type": row[3],
            "aggregate_id": row[4],
            "aggregate_version": int(row[5]),
            "event_type": row[6],
            "idempotency_key": row[7] or "",
            "payload": _load(row[8]),
            "occurred_at": row[9],
        }

    async def list_outbox(
        self,
        *,
        statuses: list[str] | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[OutboxMessage]:
        clauses: list[str] = []
        params: list[Any] = []
        clean_statuses = [str(item).strip() for item in statuses or [] if str(item).strip()]
        if clean_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        query = """SELECT message_id, event_id, run_id, topic, payload, status, attempts,
                          max_attempts, next_attempt_at, lease_owner, lease_token,
                          lease_expires_at, last_error, created_at, delivered_at
                   FROM outbox_messages"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_outbox_from_row(row) for row in rows]

    async def get_outbox_message(self, message_id: str) -> OutboxMessage | None:
        async with self.db.execute(
            """SELECT message_id, event_id, run_id, topic, payload, status, attempts,
                      max_attempts, next_attempt_at, lease_owner, lease_token,
                      lease_expires_at, last_error, created_at, delivered_at
               FROM outbox_messages WHERE message_id = ?""",
            (message_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _outbox_from_row(row) if row else None

    async def save_outbox_delivery_receipt(
        self,
        receipt: OutboxDeliveryReceipt,
    ) -> OutboxDeliveryReceipt:
        if not receipt.message_id.strip() or not receipt.consumer_id.strip():
            raise ValueError("outbox receipt message_id and consumer_id are required")
        await self.db.execute(
            """INSERT INTO outbox_delivery_receipts
               (message_id, consumer_id, event_id, status, payload, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(message_id, consumer_id) DO UPDATE SET
                   event_id = excluded.event_id,
                   status = excluded.status,
                   payload = excluded.payload,
                   completed_at = excluded.completed_at""",
            (
                receipt.message_id,
                receipt.consumer_id,
                receipt.event_id,
                receipt.status,
                _dump(receipt.to_dict()),
                receipt.created_at.isoformat(),
                receipt.completed_at.isoformat(),
            ),
        )
        await self.db.commit()
        return receipt

    async def get_outbox_delivery_receipt(
        self,
        message_id: str,
        consumer_id: str,
    ) -> OutboxDeliveryReceipt | None:
        payload = await self._payload_one(
            """SELECT payload FROM outbox_delivery_receipts
               WHERE message_id = ? AND consumer_id = ?""",
            (message_id, consumer_id),
        )
        return OutboxDeliveryReceipt.from_dict(payload) if payload else None

    async def save_learning_asset(self, asset: LearningAsset, *, commit: bool = True) -> LearningAsset:
        asset.validate()
        self._assert_project(asset.project_id)
        asset.updated_at = utc_now()
        await self.db.execute(
            """INSERT INTO learning_assets
               (asset_id, organization_id, project_id, employee_id, role_id, kind,
                name, version, status, previous_asset_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   organization_id = excluded.organization_id,
                   project_id = excluded.project_id,
                   employee_id = excluded.employee_id,
                   role_id = excluded.role_id,
                   kind = excluded.kind,
                   name = excluded.name,
                   version = excluded.version,
                   status = excluded.status,
                   previous_asset_id = excluded.previous_asset_id,
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (
                asset.asset_id,
                asset.organization_id,
                asset.project_id,
                asset.employee_id,
                asset.role_id,
                asset.kind,
                asset.name,
                asset.version,
                asset.status.value,
                asset.previous_asset_id,
                _dump(asset.to_dict()),
                asset.created_at.isoformat(),
                asset.updated_at.isoformat(),
            ),
        )
        if commit:
            await self.db.commit()
        return asset

    async def get_learning_asset(self, asset_id: str) -> LearningAsset | None:
        payload = await self._payload_one(
            "SELECT payload FROM learning_assets WHERE asset_id = ?",
            (asset_id,),
        )
        return LearningAsset.from_dict(payload) if payload else None

    async def list_learning_assets(
        self,
        *,
        project_id: str | None = None,
        organization_id: str | None = None,
        name: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[LearningAsset]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("organization_id", organization_id),
            ("name", name),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        clean_statuses = [str(item).strip() for item in statuses or [] if str(item).strip()]
        if clean_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        query = "SELECT payload FROM learning_assets"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [LearningAsset.from_dict(item) for item in await self._payload_all(query, params)]

    async def save_learning_evaluation(
        self,
        evaluation: LearningAssetEvaluation,
        *,
        commit: bool = True,
    ) -> LearningAssetEvaluation:
        if await self.get_learning_asset(evaluation.asset_id) is None:
            raise KeyError(f"learning asset not found: {evaluation.asset_id}")
        await self.db.execute(
            """INSERT INTO learning_asset_evaluations
               (evaluation_id, asset_id, phase, score, passed, payload, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(evaluation_id) DO UPDATE SET
                   asset_id = excluded.asset_id,
                   phase = excluded.phase,
                   score = excluded.score,
                   passed = excluded.passed,
                   payload = excluded.payload,
                   evaluated_at = excluded.evaluated_at""",
            (
                evaluation.evaluation_id,
                evaluation.asset_id,
                evaluation.phase,
                evaluation.score,
                int(evaluation.passed),
                _dump(evaluation.to_dict()),
                evaluation.evaluated_at.isoformat(),
            ),
        )
        if commit:
            await self.db.commit()
        return evaluation

    async def list_learning_evaluations(self, asset_id: str) -> list[LearningAssetEvaluation]:
        payloads = await self._payload_all(
            """SELECT payload FROM learning_asset_evaluations
               WHERE asset_id = ? ORDER BY evaluated_at ASC""",
            (asset_id,),
        )
        return [LearningAssetEvaluation.from_dict(item) for item in payloads]

    async def save_capability_attempt(self, attempt: CapabilityAttempt) -> CapabilityAttempt:
        self._assert_project(attempt.project_id)
        await self.db.execute(
            """INSERT INTO capability_attempts
               (attempt_id, request_id, route_id, run_id, project_id, capability_kind,
                provider, candidate_id, status, cost_usd, latency_ms, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(attempt_id) DO UPDATE SET
                   status = excluded.status,
                   cost_usd = excluded.cost_usd,
                   latency_ms = excluded.latency_ms,
                   payload = excluded.payload""",
            (
                attempt.attempt_id,
                attempt.request_id,
                attempt.route_id,
                attempt.run_id,
                attempt.project_id,
                attempt.capability_kind.value,
                attempt.provider,
                attempt.candidate_id,
                attempt.status,
                attempt.cost_usd,
                attempt.latency_ms,
                _dump(attempt.to_dict()),
                attempt.created_at.isoformat(),
            ),
        )
        await self.db.commit()
        return attempt

    async def list_capability_attempts(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[CapabilityAttempt]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        query = "SELECT payload FROM capability_attempts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [CapabilityAttempt.from_dict(item) for item in await self._payload_all(query, params)]

    async def save_route_execution_contract(
        self,
        contract: RouteExecutionContract,
    ) -> RouteExecutionContract:
        contract.validate()
        self._assert_project(contract.project_id)
        now = utc_now().isoformat()
        await self.db.execute(
            """INSERT INTO route_execution_contracts
               (contract_id, request_id, route_id, run_id, project_id, capability_kind,
                status, planned_provider, actual_provider, planned_model, actual_model,
                payload, created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(contract_id) DO UPDATE SET
                   status = excluded.status,
                   actual_provider = excluded.actual_provider,
                   actual_model = excluded.actual_model,
                   payload = excluded.payload,
                   updated_at = excluded.updated_at,
                   completed_at = excluded.completed_at""",
            (
                contract.contract_id,
                contract.request_id,
                contract.route_id,
                contract.run_id,
                contract.project_id,
                contract.capability_kind.value,
                contract.status,
                contract.planned_provider,
                contract.actual_provider,
                contract.planned_model,
                contract.actual_model,
                _dump(contract.to_dict()),
                contract.created_at.isoformat(),
                now,
                _iso(contract.completed_at),
            ),
        )
        await self.db.commit()
        return contract

    async def get_route_execution_contract(
        self,
        contract_id: str,
    ) -> RouteExecutionContract | None:
        payload = await self._payload_one(
            "SELECT payload FROM route_execution_contracts WHERE contract_id = ?",
            (contract_id,),
        )
        return RouteExecutionContract.from_dict(payload) if payload else None

    async def list_route_execution_contracts(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[RouteExecutionContract]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        clean_statuses = [str(item).strip() for item in statuses or [] if str(item).strip()]
        if clean_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        query = "SELECT payload FROM route_execution_contracts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        return [
            RouteExecutionContract.from_dict(item)
            for item in await self._payload_all(query, params)
        ]

    async def save_provider_usage_event(
        self,
        usage: ProviderUsageEvent,
    ) -> ProviderUsageEvent:
        usage.validate()
        self._assert_project(usage.project_id)
        await self.db.execute(
            """INSERT INTO provider_usage_events
               (usage_event_id, contract_id, request_id, route_id, run_id, project_id,
                capability_kind, provider, model, measured, source, total_tokens,
                cost_usd, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(usage_event_id) DO UPDATE SET
                   measured = excluded.measured,
                   source = excluded.source,
                   total_tokens = excluded.total_tokens,
                   cost_usd = excluded.cost_usd,
                   payload = excluded.payload""",
            (
                usage.usage_event_id,
                usage.contract_id,
                usage.request_id,
                usage.route_id,
                usage.run_id,
                usage.project_id,
                usage.capability_kind.value,
                usage.provider,
                usage.model,
                int(usage.measured),
                usage.source,
                usage.total_tokens,
                usage.cost_usd,
                _dump(usage.to_dict()),
                usage.created_at.isoformat(),
            ),
        )
        await self.db.commit()
        return usage

    async def list_provider_usage_events(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[ProviderUsageEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("run_id", run_id),
            ("provider", provider),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT payload FROM provider_usage_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        return [ProviderUsageEvent.from_dict(item) for item in await self._payload_all(query, params)]

    async def provider_call_quota_status(
        self,
        *,
        project_id: str,
        provider: str,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return conservative call-count usage for one rolling subscription window."""

        self._assert_project(project_id)
        checked_at = now or utc_now()
        bounded_limit = max(0, int(limit))
        bounded_window = max(1, int(window_seconds))
        cutoff = checked_at - timedelta(seconds=bounded_window)
        async with self.db.execute(
            """SELECT COUNT(*) FROM provider_call_reservations
               WHERE project_id = ? AND provider = ? AND created_at >= ?
                 AND status != 'released'""",
            (project_id, str(provider), cutoff.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        used = int(row[0] if row else 0)
        return {
            "project_id": project_id,
            "provider": str(provider),
            "limit": bounded_limit,
            "used": used,
            "remaining": max(0, bounded_limit - used),
            "window_seconds": bounded_window,
            "window_started_at": cutoff.isoformat(),
            "checked_at": checked_at.isoformat(),
            "allowed": bounded_limit == 0 or used < bounded_limit,
            "enabled": bounded_limit > 0,
        }

    async def list_provider_call_quota_providers(
        self,
        *,
        project_id: str,
    ) -> list[str]:
        self._assert_project(project_id)
        async with self.db.execute(
            """SELECT DISTINCT provider FROM provider_call_reservations
               WHERE project_id = ? ORDER BY provider""",
            (project_id,),
        ) as cursor:
            return [str(row[0]) for row in await cursor.fetchall() if str(row[0]).strip()]

    async def reserve_provider_call(
        self,
        *,
        contract_id: str,
        request_id: str,
        project_id: str,
        provider: str,
        model: str,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve one subscription call before provider I/O."""

        self._assert_project(project_id)
        normalized_provider = str(provider or "").strip()
        if not contract_id.strip() or not normalized_provider:
            raise ValueError("contract_id and provider are required for call reservation")
        checked_at = now or utc_now()
        bounded_limit = max(0, int(limit))
        bounded_window = max(1, int(window_seconds))
        reservation_id = str(uuid.uuid4())
        cutoff = checked_at - timedelta(seconds=bounded_window)
        async with self.transaction_lock:
            async with self.db.execute("BEGIN IMMEDIATE") as cursor:
                if cursor is None:
                    raise RuntimeError("could not begin provider quota transaction")
            try:
                async with self.db.execute(
                    """SELECT COUNT(*) FROM provider_call_reservations
                       WHERE project_id = ? AND provider = ? AND created_at >= ?
                         AND status != 'released'""",
                    (project_id, normalized_provider, cutoff.isoformat()),
                ) as cursor:
                    row = await cursor.fetchone()
                used = int(row[0] if row else 0)
                if bounded_limit > 0 and used >= bounded_limit:
                    raise ProviderCallQuotaExceeded(
                        f"subscription call quota exhausted for {normalized_provider}: "
                        f"{used}/{bounded_limit} calls in {bounded_window}s"
                    )
                await self.db.execute(
                    """INSERT INTO provider_call_reservations
                       (reservation_id, contract_id, request_id, project_id, provider,
                        model, status, window_seconds, created_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, NULL)""",
                    (
                        reservation_id,
                        contract_id,
                        request_id,
                        project_id,
                        normalized_provider,
                        str(model or ""),
                        bounded_window,
                        checked_at.isoformat(),
                    ),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return {
            "reservation_id": reservation_id,
            "provider": normalized_provider,
            "limit": bounded_limit,
            "used": used + 1,
            "remaining": max(0, bounded_limit - used - 1),
            "window_seconds": bounded_window,
            "created_at": checked_at.isoformat(),
        }

    async def finish_provider_call_reservation(
        self,
        reservation_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
    ) -> None:
        normalized = str(reservation_id or "").strip()
        if not normalized:
            return
        await self.db.execute(
            """UPDATE provider_call_reservations
               SET status = ?, completed_at = ? WHERE reservation_id = ?""",
            (
                str(status or "completed")[:80],
                (completed_at or utc_now()).isoformat(),
                normalized,
            ),
        )
        await self.db.commit()

    async def save_provider_canary_result(
        self,
        result: ProviderCanaryResult,
    ) -> ProviderCanaryResult:
        self._assert_project(result.project_id)
        await self.db.execute(
            """INSERT INTO provider_canary_results
               (canary_id, project_id, capability_kind, provider, candidate_id,
                model, mode, success, latency_ms, model_drift, error_category,
                payload, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(canary_id) DO UPDATE SET
                   success = excluded.success,
                   latency_ms = excluded.latency_ms,
                   model_drift = excluded.model_drift,
                   error_category = excluded.error_category,
                   payload = excluded.payload,
                   checked_at = excluded.checked_at""",
            (
                result.canary_id,
                result.project_id,
                result.capability_kind.value,
                result.provider,
                result.candidate_id,
                result.model,
                result.mode,
                int(result.success),
                result.latency_ms,
                int(result.model_drift),
                result.error_category,
                _dump(result.to_dict()),
                result.checked_at.isoformat(),
            ),
        )
        await self.db.commit()
        return result

    async def list_provider_canary_results(
        self,
        *,
        project_id: str | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[ProviderCanaryResult]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        query = "SELECT payload FROM provider_canary_results"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY checked_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        return [ProviderCanaryResult.from_dict(item) for item in await self._payload_all(query, params)]

    async def consume_resource_approval(
        self,
        *,
        token_digest: str,
        project_id: str,
        candidate_id: str,
        request_id: str,
        claims: Mapping[str, Any],
    ) -> None:
        """Consume a signed live-resource approval exactly once."""

        self._assert_project(project_id)
        normalized = str(token_digest or "").strip()
        if not normalized:
            raise ValueError("resource approval token digest is required")
        async with self.transaction_lock:
            async with self.db.execute("BEGIN IMMEDIATE") as cursor:
                if cursor is None:
                    raise RuntimeError("could not begin resource approval transaction")
            try:
                async with self.db.execute(
                    "SELECT 1 FROM resource_approval_uses WHERE token_digest = ?",
                    (normalized,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    raise PermissionError("resource approval token has already been consumed")
                await self.db.execute(
                    """INSERT INTO resource_approval_uses
                       (token_digest, approval_id, operator_id, key_id, project_id,
                        candidate_id, request_id, claims, consumed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        normalized,
                        str(claims.get("approval_id", "") or ""),
                        str(claims.get("operator_id", "") or ""),
                        str(claims.get("key_id", "") or ""),
                        project_id,
                        candidate_id,
                        request_id,
                        _dump(dict(claims)),
                        utc_now().isoformat(),
                    ),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def save_staffing_decision(self, decision: StaffingDecision) -> StaffingDecision:
        self._assert_project(decision.project_id)
        decision.updated_at = utc_now()
        await self.db.execute(
            """INSERT INTO staffing_decisions
               (decision_id, run_id, project_id, role_id, selected_employee_id,
                predicted_score, observed_score, regret, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id) DO UPDATE SET
                   run_id = excluded.run_id,
                   project_id = excluded.project_id,
                   role_id = excluded.role_id,
                   selected_employee_id = excluded.selected_employee_id,
                   predicted_score = excluded.predicted_score,
                   observed_score = excluded.observed_score,
                   regret = excluded.regret,
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (
                decision.decision_id,
                decision.run_id,
                decision.project_id,
                decision.role_id,
                decision.selected_employee_id,
                decision.predicted_score,
                decision.observed_score,
                decision.regret,
                _dump(decision.to_dict()),
                decision.created_at.isoformat(),
                decision.updated_at.isoformat(),
            ),
        )
        await self.db.commit()
        return decision

    async def get_staffing_decision(self, decision_id: str) -> StaffingDecision | None:
        payload = await self._payload_one(
            "SELECT payload FROM staffing_decisions WHERE decision_id = ?",
            (decision_id,),
        )
        return StaffingDecision.from_dict(payload) if payload else None

    async def list_staffing_decisions(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        role_id: str | None = None,
        limit: int = 100,
    ) -> list[StaffingDecision]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("run_id", run_id), ("project_id", project_id), ("role_id", role_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT payload FROM staffing_decisions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [StaffingDecision.from_dict(item) for item in await self._payload_all(query, params)]

    async def save_operator_action(self, action: OperatorAction) -> OperatorAction:
        action.validate()
        self._assert_project(action.project_id)
        await self.db.execute(
            """INSERT INTO operator_actions
               (action_id, project_id, kind, target_id, status, plan_digest,
                idempotency_key, operator_id, payload, created_at, expires_at,
                executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(action_id) DO UPDATE SET
                   status = excluded.status,
                   operator_id = excluded.operator_id,
                   payload = excluded.payload,
                   executed_at = excluded.executed_at""",
            (
                action.action_id,
                action.project_id,
                action.kind,
                action.target_id,
                action.status,
                action.plan_digest,
                action.idempotency_key or None,
                action.operator_id,
                _dump(action.to_dict()),
                action.created_at.isoformat(),
                _iso(action.expires_at),
                _iso(action.executed_at),
            ),
        )
        await self.db.commit()
        return action

    async def get_operator_action(self, action_id: str) -> OperatorAction | None:
        payload = await self._payload_one(
            "SELECT payload FROM operator_actions WHERE action_id = ?",
            (action_id,),
        )
        return OperatorAction.from_dict(payload) if payload else None

    async def get_operator_action_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OperatorAction | None:
        if not idempotency_key:
            return None
        payload = await self._payload_one(
            "SELECT payload FROM operator_actions WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        return OperatorAction.from_dict(payload) if payload else None

    async def list_operator_actions(
        self,
        *,
        project_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[OperatorAction]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        clean_statuses = [str(item).strip() for item in statuses or [] if str(item).strip()]
        if clean_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in clean_statuses)})")
            params.extend(clean_statuses)
        query = "SELECT payload FROM operator_actions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [
            OperatorAction.from_dict(item)
            for item in await self._payload_all(query, params)
        ]

    async def count_rows(self, table: str, *, where: str = "", params: tuple[Any, ...] = ()) -> int:
        allowed = {
            "goal_contracts",
            "goal_contract_versions",
            "run_manifests",
            "run_scorecards",
            "operating_events",
            "outbox_messages",
            "outbox_delivery_receipts",
            "learning_assets",
            "capability_attempts",
            "route_execution_contracts",
            "provider_usage_events",
            "provider_canary_results",
            "resource_approval_uses",
            "provider_call_reservations",
            "staffing_decisions",
            "operator_actions",
        }
        if table not in allowed:
            raise ValueError(f"unsupported operations table: {table}")
        query = f"SELECT COUNT(*) FROM {table}"
        if where:
            query += f" WHERE {where}"
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def _payload_one(self, query: str, params: tuple[Any, ...] | list[Any]) -> dict[str, Any] | None:
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return _load(row[0]) if row else None

    async def _payload_all(self, query: str, params: tuple[Any, ...] | list[Any]) -> list[dict[str, Any]]:
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_load(row[0]) for row in rows]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _outbox_from_row(row: Any) -> OutboxMessage:
    return OutboxMessage.from_dict(
        {
            "message_id": row[0],
            "event_id": row[1],
            "run_id": row[2],
            "topic": row[3],
            "payload": _load(row[4]),
            "status": row[5],
            "attempts": row[6],
            "max_attempts": row[7],
            "next_attempt_at": row[8],
            "lease_owner": row[9],
            "lease_token": row[10],
            "lease_expires_at": row[11],
            "last_error": row[12],
            "created_at": row[13],
            "delivered_at": row[14],
        }
    )
