"""Durable run events, transactional outbox, leases, and recovery guards."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from opc.core.config import DurableOperationsConfig
from opc.operations.models import (
    OperatingEvent,
    OutboxMessage,
    ResourceBudget,
    RunLease,
    RunManifest,
    RunMetrics,
    RunStatus,
    parse_datetime,
    utc_now,
)
from opc.operations.repository import OperationsRepository


class DurableOperationsError(RuntimeError):
    """Base error for durable-kernel invariants."""


class LeaseUnavailable(DurableOperationsError):
    """Raised when another live owner holds the requested run lease."""


class StaleFencingToken(DurableOperationsError):
    """Raised when a worker attempts to write after losing ownership."""


class AggregateVersionConflict(DurableOperationsError):
    """Raised when optimistic aggregate version comparison fails."""


class BudgetExceeded(DurableOperationsError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


@dataclass(frozen=True)
class AppendResult:
    event: OperatingEvent
    outbox: OutboxMessage | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class DeadlockReport:
    run_id: str
    deadlocked: bool
    inactivity_seconds: float
    threshold_seconds: float
    last_event_type: str = ""
    last_activity_at: datetime | None = None
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "deadlocked": self.deadlocked,
            "inactivity_seconds": self.inactivity_seconds,
            "threshold_seconds": self.threshold_seconds,
            "last_event_type": self.last_event_type,
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class RecoveryReport:
    run_id: str
    lease: RunLease
    recovered_outbox: int
    deadlock: DeadlockReport
    actions: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lease": self.lease.to_dict(),
            "recovered_outbox": self.recovered_outbox,
            "deadlock": self.deadlock.to_dict(),
            "actions": list(self.actions),
        }


class DurableRunKernel:
    """SQLite-backed execution kernel with compare-and-fence semantics."""

    def __init__(
        self,
        repository: OperationsRepository,
        config: DurableOperationsConfig | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or DurableOperationsConfig()

    async def record_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        aggregate_type: str = "run",
        aggregate_id: str = "",
        expected_version: int | None = None,
        idempotency_key: str = "",
        outbox_topic: str = "",
        outbox_payload: Mapping[str, Any] | None = None,
        lease_owner: str = "",
        fencing_token: int | None = None,
        metrics: RunMetrics | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AppendResult:
        """Append an event and optional outbox row in one committed transaction."""
        manifest = await self.repository.get_manifest(run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {run_id}")
        if metrics is not None:
            await self.check_budget(run_id, metrics)
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                result = await self._append_event_locked(
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    outbox_topic=outbox_topic,
                    outbox_payload=outbox_payload,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    timestamp=timestamp,
                )
                await self.repository.db.commit()
                return result
            except Exception:
                await self.repository.db.rollback()
                raise

    async def start_run(
        self,
        manifest: RunManifest,
        *,
        now: datetime | None = None,
    ) -> tuple[RunManifest, AppendResult]:
        """Persist a running manifest and its first event atomically and idempotently."""
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                existing = await self.repository.get_manifest(manifest.run_id)
                if existing is not None:
                    if (
                        existing.goal_id != manifest.goal_id
                        or existing.project_id != manifest.project_id
                    ):
                        raise ValueError("existing run manifest identity does not match start request")
                    if existing.status in {
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                        RunStatus.CANCELLED,
                    }:
                        raise DurableOperationsError(
                            f"cannot start terminal run {manifest.run_id}: {existing.status.value}"
                        )
                    active = existing
                else:
                    active = manifest
                if active.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                    raise DurableOperationsError(
                        f"cannot start run {active.run_id} from {active.status.value}"
                    )
                active.status = RunStatus.RUNNING
                if active.started_at is None:
                    active.started_at = timestamp
                await self.repository.save_manifest(active, commit=False)
                result = await self._append_event_locked(
                    run_id=active.run_id,
                    event_type="run.started",
                    payload={"goal_id": active.goal_id},
                    idempotency_key=f"run-started:{active.run_id}",
                    timestamp=timestamp,
                )
                await self.repository.db.commit()
                return active, result
            except Exception:
                await self.repository.db.rollback()
                raise

    async def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        now: datetime | None = None,
    ) -> tuple[RunManifest, AppendResult]:
        """Persist a terminal manifest, event, and delivery row in one transaction."""
        if status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ValueError("finish status must be completed, failed, or cancelled")
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                manifest = await self.repository.get_manifest(run_id)
                if manifest is None:
                    raise KeyError(f"run manifest not found: {run_id}")
                if manifest.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                } and manifest.status != status:
                    raise DurableOperationsError(
                        f"run {run_id} already ended as {manifest.status.value}"
                    )
                manifest.status = status
                if manifest.completed_at is None:
                    manifest.completed_at = timestamp
                await self.repository.save_manifest(manifest, commit=False)
                result = await self._append_event_locked(
                    run_id=manifest.run_id,
                    event_type=f"run.{status.value}",
                    payload={"goal_id": manifest.goal_id},
                    idempotency_key=f"run-finished:{manifest.run_id}:{status.value}",
                    outbox_topic="operations.run.lifecycle",
                    timestamp=timestamp,
                )
                await self.repository.db.commit()
                return manifest, result
            except Exception:
                await self.repository.db.rollback()
                raise

    async def acquire_lease(
        self,
        run_id: str,
        *,
        owner: str,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RunLease:
        if await self.repository.get_manifest(run_id) is None:
            raise KeyError(f"run manifest not found: {run_id}")
        clean_owner = str(owner or "").strip()
        if not clean_owner:
            raise ValueError("lease owner is required")
        timestamp = now or utc_now()
        duration = max(1, int(lease_seconds or self.config.lease_seconds))
        expires_at = timestamp + timedelta(seconds=duration)
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                row = await self._lease_row(run_id)
                if row is None:
                    token = 1
                else:
                    existing_owner = str(row[1])
                    existing_token = int(row[2])
                    existing_expiry = parse_datetime(row[3], default=timestamp) or timestamp
                    if existing_expiry > timestamp and existing_owner != clean_owner:
                        raise LeaseUnavailable(
                            f"run {run_id} is leased by {existing_owner} until {existing_expiry.isoformat()}"
                        )
                    token = existing_token if existing_owner == clean_owner and existing_expiry > timestamp else existing_token + 1
                await self.repository.db.execute(
                    """INSERT INTO run_leases(run_id, lease_owner, fencing_token, expires_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                           lease_owner = excluded.lease_owner,
                           fencing_token = excluded.fencing_token,
                           expires_at = excluded.expires_at,
                           updated_at = excluded.updated_at""",
                    (run_id, clean_owner, token, expires_at.isoformat(), timestamp.isoformat()),
                )
                await self.repository.db.commit()
            except Exception:
                await self.repository.db.rollback()
                raise
        return RunLease(
            run_id=run_id,
            owner=clean_owner,
            fencing_token=token,
            expires_at=expires_at,
            updated_at=timestamp,
        )

    async def renew_lease(
        self,
        lease: RunLease,
        *,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RunLease:
        timestamp = now or utc_now()
        expires_at = timestamp + timedelta(seconds=max(1, int(lease_seconds or self.config.lease_seconds)))
        async with self.repository.transaction_lock:
            async with self.repository.db.execute(
                """UPDATE run_leases
                   SET expires_at = ?, updated_at = ?
                   WHERE run_id = ? AND lease_owner = ? AND fencing_token = ? AND expires_at > ?""",
                (
                    expires_at.isoformat(),
                    timestamp.isoformat(),
                    lease.run_id,
                    lease.owner,
                    lease.fencing_token,
                    timestamp.isoformat(),
                ),
            ) as cursor:
                changed = cursor.rowcount
            if changed != 1:
                await self.repository.db.rollback()
                raise StaleFencingToken(f"cannot renew stale lease for run {lease.run_id}")
            await self.repository.db.commit()
        return RunLease(
            run_id=lease.run_id,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            expires_at=expires_at,
            updated_at=timestamp,
        )

    async def release_lease(
        self,
        lease: RunLease,
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            async with self.repository.db.execute(
                """UPDATE run_leases
                   SET expires_at = ?, updated_at = ?
                   WHERE run_id = ? AND lease_owner = ? AND fencing_token = ?""",
                (
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    lease.run_id,
                    lease.owner,
                    lease.fencing_token,
                ),
            ) as cursor:
                changed = cursor.rowcount
            if changed != 1:
                await self.repository.db.rollback()
                raise StaleFencingToken(f"cannot release stale lease for run {lease.run_id}")
            await self.repository.db.commit()

    async def assert_fence(
        self,
        run_id: str,
        *,
        owner: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> None:
        await self._assert_fence_locked(
            run_id,
            owner=owner,
            fencing_token=fencing_token,
            now=now or utc_now(),
        )

    async def claim_outbox(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> list[OutboxMessage]:
        clean_worker = str(worker_id or "").strip()
        if not clean_worker:
            raise ValueError("worker_id is required")
        timestamp = now or utc_now()
        expires_at = timestamp + timedelta(seconds=max(1, int(lease_seconds or self.config.lease_seconds)))
        bounded_limit = max(1, min(int(limit), 500))
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                await self._recover_expired_outbox_locked(timestamp)
                async with self.repository.db.execute(
                    """SELECT message_id FROM outbox_messages
                       WHERE status = 'pending' AND next_attempt_at <= ?
                       ORDER BY created_at ASC LIMIT ?""",
                    (timestamp.isoformat(), bounded_limit),
                ) as cursor:
                    ids = [str(row[0]) for row in await cursor.fetchall()]
                claimed: list[OutboxMessage] = []
                for message_id in ids:
                    await self.repository.db.execute(
                        """UPDATE outbox_messages
                           SET status = 'processing', attempts = attempts + 1,
                               lease_owner = ?, lease_token = lease_token + 1,
                               lease_expires_at = ?, last_error = ''
                           WHERE message_id = ? AND status = 'pending'""",
                        (clean_worker, expires_at.isoformat(), message_id),
                    )
                    message = await self._outbox_by_id(message_id)
                    if message is not None and message.status == "processing":
                        claimed.append(message)
                await self.repository.db.commit()
                return claimed
            except Exception:
                await self.repository.db.rollback()
                raise

    async def acknowledge_outbox(
        self,
        message_id: str,
        *,
        worker_id: str,
        lease_token: int,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            async with self.repository.db.execute(
                """UPDATE outbox_messages
                   SET status = 'delivered', delivered_at = ?, lease_owner = '',
                       lease_expires_at = NULL, last_error = ''
                   WHERE message_id = ? AND status = 'processing'
                     AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?""",
                (
                    timestamp.isoformat(),
                    message_id,
                    worker_id,
                    int(lease_token),
                    timestamp.isoformat(),
                ),
            ) as cursor:
                changed = cursor.rowcount
            if changed != 1:
                await self.repository.db.rollback()
                raise StaleFencingToken(f"outbox acknowledgement lost lease: {message_id}")
            await self.repository.db.commit()

    async def fail_outbox(
        self,
        message_id: str,
        *,
        worker_id: str,
        lease_token: int,
        error: str,
        now: datetime | None = None,
    ) -> OutboxMessage:
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                message = await self._outbox_by_id(message_id)
                if (
                    message is None
                    or message.status != "processing"
                    or message.lease_owner != worker_id
                    or message.lease_token != int(lease_token)
                    or message.lease_expires_at is None
                    or message.lease_expires_at <= timestamp
                ):
                    raise StaleFencingToken(f"outbox failure update lost lease: {message_id}")
                if message.attempts >= message.max_attempts:
                    status = "dead_letter"
                    next_attempt = timestamp
                else:
                    status = "pending"
                    delay = min(
                        self.config.retry_max_seconds,
                        self.config.retry_base_seconds * (2 ** max(0, message.attempts - 1)),
                    )
                    next_attempt = timestamp + timedelta(seconds=delay)
                await self.repository.db.execute(
                    """UPDATE outbox_messages
                       SET status = ?, next_attempt_at = ?, lease_owner = '',
                           lease_expires_at = NULL, last_error = ?
                       WHERE message_id = ? AND status = 'processing'
                         AND lease_owner = ? AND lease_token = ?""",
                    (
                        status,
                        next_attempt.isoformat(),
                        str(error or "")[:4000],
                        message_id,
                        worker_id,
                        int(lease_token),
                    ),
                )
                updated = await self._outbox_by_id(message_id)
                await self.repository.db.commit()
                assert updated is not None
                return updated
            except Exception:
                await self.repository.db.rollback()
                raise

    async def recover_expired_outbox(self, *, now: datetime | None = None) -> int:
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                recovered = await self._recover_expired_outbox_locked(timestamp)
                await self.repository.db.commit()
                return recovered
            except Exception:
                await self.repository.db.rollback()
                raise

    async def replay_dead_letter(
        self,
        message_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[OutboxMessage, OperatingEvent]:
        """Explicitly requeue one dead letter and append an audit event atomically."""
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("dead-letter replay reason is required")
        timestamp = now or utc_now()
        async with self.repository.transaction_lock:
            await self._begin()
            try:
                message = await self._outbox_by_id(message_id)
                if message is None:
                    raise KeyError(f"outbox message not found: {message_id}")
                if message.status != "dead_letter":
                    raise DurableOperationsError(
                        f"outbox message {message_id} is {message.status}, not dead_letter"
                    )
                await self.repository.db.execute(
                    """UPDATE outbox_messages
                       SET status = 'pending', attempts = 0, next_attempt_at = ?,
                           lease_owner = '', lease_expires_at = NULL,
                           delivered_at = NULL, last_error = ?
                       WHERE message_id = ? AND status = 'dead_letter'""",
                    (
                        timestamp.isoformat(),
                        f"operator replay: {clean_reason}"[:4000],
                        message_id,
                    ),
                )
                audit = await self._append_event_locked(
                    run_id=message.run_id,
                    event_type="outbox.replayed",
                    payload={"message_id": message_id, "reason": clean_reason},
                    aggregate_type="outbox",
                    aggregate_id=message_id,
                    timestamp=timestamp,
                )
                updated = await self._outbox_by_id(message_id)
                assert updated is not None
                await self.repository.db.commit()
                return updated, audit.event
            except Exception:
                await self.repository.db.rollback()
                raise

    async def check_budget(
        self,
        run_id: str,
        metrics: RunMetrics | Mapping[str, Any],
    ) -> None:
        manifest = await self.repository.get_manifest(run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {run_id}")
        goal = (
            await self.repository.get_goal_version(
                manifest.goal_id,
                manifest.goal_version,
            )
            if manifest.goal_version
            else await self.repository.get_goal(manifest.goal_id)
        )
        if goal is None:
            raise KeyError(
                f"goal contract version not found: "
                f"{manifest.goal_id}@{manifest.goal_version}"
            )
        parsed = metrics if isinstance(metrics, RunMetrics) else RunMetrics.from_dict(metrics)
        parsed.validate()
        violations = _budget_violations(goal.budget, parsed)
        if violations:
            raise BudgetExceeded(violations)

    async def detect_deadlock(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
        threshold_seconds: int | None = None,
    ) -> DeadlockReport:
        manifest = await self.repository.get_manifest(run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {run_id}")
        timestamp = now or utc_now()
        threshold = float(max(1, int(threshold_seconds or self.config.deadlock_after_seconds)))
        events = await self.repository.list_events(run_id, limit=10_000)
        if events:
            last = events[-1]
            last_activity = parse_datetime(last.get("occurred_at"), default=manifest.started_at or manifest.created_at)
            last_event_type = str(last.get("event_type", ""))
        else:
            last_activity = manifest.started_at or manifest.created_at
            last_event_type = ""
        assert last_activity is not None
        inactivity = max(0.0, (timestamp - last_activity).total_seconds())
        active = manifest.status in {RunStatus.RUNNING, RunStatus.BLOCKED}
        deadlocked = active and inactivity >= threshold
        blockers: list[str] = []
        if manifest.status == RunStatus.BLOCKED:
            blockers.append("run status is blocked")
        if deadlocked:
            blockers.append(f"no durable progress for {inactivity:.1f} seconds")
        return DeadlockReport(
            run_id=run_id,
            deadlocked=deadlocked,
            inactivity_seconds=inactivity,
            threshold_seconds=threshold,
            last_event_type=last_event_type,
            last_activity_at=last_activity,
            blockers=tuple(blockers),
        )

    async def recover_run(
        self,
        run_id: str,
        *,
        owner: str,
        metrics: RunMetrics | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RecoveryReport:
        timestamp = now or utc_now()
        if metrics is not None:
            await self.check_budget(run_id, metrics)
        deadlock = await self.detect_deadlock(run_id, now=timestamp)
        lease = await self.acquire_lease(run_id, owner=owner, now=timestamp)
        recovered = await self.recover_expired_outbox(now=timestamp)
        actions: list[str] = []
        if recovered:
            actions.append(f"requeued {recovered} expired outbox message(s)")
        if deadlock.deadlocked:
            await self.record_event(
                run_id=run_id,
                event_type="run.deadlock_detected",
                payload=deadlock.to_dict(),
                idempotency_key=f"deadlock:{run_id}:{int(timestamp.timestamp()) // max(1, self.config.deadlock_after_seconds)}",
                outbox_topic="operations.alert",
                lease_owner=lease.owner,
                fencing_token=lease.fencing_token,
                now=timestamp,
            )
            actions.append("recorded deadlock alert")
        return RecoveryReport(
            run_id=run_id,
            lease=lease,
            recovered_outbox=recovered,
            deadlock=deadlock,
            actions=tuple(actions),
        )

    async def _begin(self) -> None:
        try:
            async with self.repository.db.execute("BEGIN IMMEDIATE"):
                return
        except sqlite3.OperationalError as exc:
            raise DurableOperationsError(f"could not begin durable transaction: {exc}") from exc

    async def _append_event_locked(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        aggregate_type: str = "run",
        aggregate_id: str = "",
        expected_version: int | None = None,
        idempotency_key: str = "",
        outbox_topic: str = "",
        outbox_payload: Mapping[str, Any] | None = None,
        lease_owner: str = "",
        fencing_token: int | None = None,
        timestamp: datetime,
    ) -> AppendResult:
        normalized_type = str(event_type or "").strip()
        if not normalized_type:
            raise ValueError("event_type is required")
        normalized_aggregate_id = str(aggregate_id or run_id).strip()
        if not normalized_aggregate_id:
            raise ValueError("aggregate_id is required")
        normalized_topic = str(outbox_topic or "").strip()
        if outbox_topic and not normalized_topic:
            raise ValueError("outbox_topic cannot be whitespace")
        if idempotency_key:
            duplicate = await self._event_by_idempotency_key(idempotency_key)
            if duplicate is not None:
                if (
                    duplicate.run_id != run_id
                    or duplicate.event_type != normalized_type
                    or duplicate.aggregate_type != aggregate_type
                    or duplicate.aggregate_id != normalized_aggregate_id
                ):
                    raise DurableOperationsError(
                        "idempotency key collision across a different event identity: "
                        f"{idempotency_key!r}"
                    )
                outbox = await self._outbox_for_event(duplicate.event_id)
                return AppendResult(event=duplicate, outbox=outbox, duplicate=True)
        if fencing_token is not None or lease_owner:
            if fencing_token is None or not lease_owner:
                raise ValueError("lease_owner and fencing_token must be supplied together")
            await self._assert_fence_locked(
                run_id,
                owner=lease_owner,
                fencing_token=fencing_token,
                now=timestamp,
            )
        current_version = await self._aggregate_version(
            aggregate_type,
            normalized_aggregate_id,
        )
        if expected_version is not None and int(expected_version) != current_version:
            raise AggregateVersionConflict(
                f"aggregate {aggregate_type}/{normalized_aggregate_id} version is "
                f"{current_version}, expected {expected_version}"
            )
        event = OperatingEvent(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            event_type=normalized_type,
            payload=dict(payload or {}),
            aggregate_type=aggregate_type,
            aggregate_id=normalized_aggregate_id,
            aggregate_version=current_version + 1,
            idempotency_key=idempotency_key,
            occurred_at=timestamp,
        )
        await self.repository.db.execute(
            """INSERT INTO operating_events
               (event_id, run_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, idempotency_key, payload, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?)""",
            (
                event.event_id,
                event.run_id,
                event.aggregate_type,
                event.aggregate_id,
                event.aggregate_version,
                event.event_type,
                event.idempotency_key,
                _dump(event.payload),
                event.occurred_at.isoformat(),
            ),
        )
        outbox: OutboxMessage | None = None
        if normalized_topic:
            outbox = OutboxMessage(
                message_id=str(uuid.uuid4()),
                event_id=event.event_id,
                run_id=run_id,
                topic=normalized_topic,
                payload=dict(outbox_payload if outbox_payload is not None else event.payload),
                max_attempts=self.config.outbox_max_attempts,
                next_attempt_at=timestamp,
                created_at=timestamp,
            )
            await self.repository.db.execute(
                """INSERT INTO outbox_messages
                   (message_id, event_id, run_id, topic, payload, status, attempts,
                    max_attempts, next_attempt_at, lease_owner, lease_token,
                    lease_expires_at, last_error, created_at, delivered_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, '', 0, NULL, '', ?, NULL)""",
                (
                    outbox.message_id,
                    outbox.event_id,
                    outbox.run_id,
                    outbox.topic,
                    _dump(outbox.payload),
                    outbox.max_attempts,
                    outbox.next_attempt_at.isoformat(),
                    outbox.created_at.isoformat(),
                ),
            )
        return AppendResult(event=event, outbox=outbox)

    async def _aggregate_version(self, aggregate_type: str, aggregate_id: str) -> int:
        async with self.repository.db.execute(
            """SELECT COALESCE(MAX(aggregate_version), 0) FROM operating_events
               WHERE aggregate_type = ? AND aggregate_id = ?""",
            (aggregate_type, aggregate_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def _event_by_idempotency_key(self, key: str) -> OperatingEvent | None:
        async with self.repository.db.execute(
            """SELECT event_id, run_id, aggregate_type, aggregate_id, aggregate_version,
                      event_type, idempotency_key, payload, occurred_at
               FROM operating_events WHERE idempotency_key = ?""",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        return _event_from_row(row) if row else None

    async def _outbox_for_event(self, event_id: str) -> OutboxMessage | None:
        async with self.repository.db.execute(
            f"""SELECT {_OUTBOX_COLUMNS} FROM outbox_messages
                WHERE event_id = ? ORDER BY created_at ASC LIMIT 1""",
            (event_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _outbox_from_row(row) if row else None

    async def _outbox_by_id(self, message_id: str) -> OutboxMessage | None:
        async with self.repository.db.execute(
            f"SELECT {_OUTBOX_COLUMNS} FROM outbox_messages WHERE message_id = ?",
            (message_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _outbox_from_row(row) if row else None

    async def _lease_row(self, run_id: str) -> Any:
        async with self.repository.db.execute(
            """SELECT run_id, lease_owner, fencing_token, expires_at, updated_at
               FROM run_leases WHERE run_id = ?""",
            (run_id,),
        ) as cursor:
            return await cursor.fetchone()

    async def _assert_fence_locked(
        self,
        run_id: str,
        *,
        owner: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        row = await self._lease_row(run_id)
        if row is None:
            raise StaleFencingToken(f"run {run_id} has no active lease")
        expiry = parse_datetime(row[3], default=now) or now
        if str(row[1]) != owner or int(row[2]) != int(fencing_token) or expiry <= now:
            raise StaleFencingToken(
                f"stale fencing token for run {run_id}: owner={owner!r} token={fencing_token}"
            )

    async def _recover_expired_outbox_locked(self, now: datetime) -> int:
        async with self.repository.db.execute(
            """SELECT message_id, attempts, max_attempts FROM outbox_messages
               WHERE status = 'processing' AND lease_expires_at <= ?""",
            (now.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        for message_id, attempts, max_attempts in rows:
            status = "dead_letter" if int(attempts) >= int(max_attempts) else "pending"
            await self.repository.db.execute(
                """UPDATE outbox_messages
                   SET status = ?, lease_owner = '', lease_expires_at = NULL,
                       next_attempt_at = ?, last_error =
                           CASE WHEN last_error = '' THEN 'delivery lease expired' ELSE last_error END
                   WHERE message_id = ? AND status = 'processing'""",
                (status, now.isoformat(), message_id),
            )
        return len(rows)


_OUTBOX_COLUMNS = """message_id, event_id, run_id, topic, payload, status, attempts,
max_attempts, next_attempt_at, lease_owner, lease_token, lease_expires_at,
last_error, created_at, delivered_at"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _event_from_row(row: Any) -> OperatingEvent:
    return OperatingEvent.from_dict(
        {
            "event_id": row[0],
            "run_id": row[1],
            "aggregate_type": row[2],
            "aggregate_id": row[3],
            "aggregate_version": row[4],
            "event_type": row[5],
            "idempotency_key": row[6] or "",
            "payload": _load(row[7]),
            "occurred_at": row[8],
        }
    )


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


def _budget_violations(budget: ResourceBudget, metrics: RunMetrics) -> list[str]:
    checks = (
        ("cost_usd", metrics.cost_usd, budget.max_cost_usd),
        ("duration_seconds", metrics.duration_seconds, budget.max_duration_seconds),
        ("tokens", metrics.tokens, budget.max_tokens),
        ("interventions", metrics.interventions, budget.max_interventions),
        ("rework_cycles", metrics.rework_cycles, budget.max_rework_cycles),
        ("failed_attempts", metrics.failed_attempts, budget.max_failed_attempts),
    )
    return [
        f"budget {name} exceeded: {actual} > {maximum}"
        for name, actual, maximum in checks
        if maximum is not None and actual > maximum
    ]
