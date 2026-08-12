from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.config import DurableOperationsConfig
from opc.database.store import OPCStore
from opc.operations.backup import OperationsBackupError, OperationsBackupManager
from opc.operations.durable import DurableRunKernel
from opc.operations.models import AcceptanceCriterion, GoalContract, RunManifest, RunStatus
from opc.operations.repository import OperationsRepository


class OperationsBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_backup_restores_complete_operations_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            live_path = root / "live.db"
            backup_path = root / "backups" / "snapshot.db"
            restored_path = root / "restored.db"
            store = OPCStore(live_path)
            await store.initialize()
            repository = OperationsRepository(store)
            kernel = DurableRunKernel(repository, DurableOperationsConfig())
            await repository.save_goal(
                GoalContract(
                    goal_id="backup-goal",
                    title="Backup state",
                    objective="Restore every durable row",
                    acceptance_criteria=[
                        AcceptanceCriterion("restore", "State is readable after restore")
                    ],
                )
            )
            await repository.save_manifest(
                RunManifest(
                    run_id="backup-run",
                    goal_id="backup-goal",
                    status=RunStatus.RUNNING,
                )
            )
            await kernel.record_event(
                run_id="backup-run",
                event_type="backup.ready",
                outbox_topic="backup.test",
            )
            manager = OperationsBackupManager(live_path)
            created = await manager.create_backup(backup_path)
            await store.close()

            restored = await manager.restore_backup(backup_path, restored_path)
            reopened = OPCStore(restored_path)
            await reopened.initialize()
            try:
                restored_repository = OperationsRepository(reopened)
                goal = await restored_repository.get_goal("backup-goal")
                events = await restored_repository.list_events("backup-run")
                outbox = await restored_repository.list_outbox(run_id="backup-run")
            finally:
                await reopened.close()

            self.assertEqual(created["integrity"], "ok")
            self.assertEqual(created["operations_schema_version"], 4)
            self.assertTrue(Path(f"{backup_path}.manifest.json").is_file())
            self.assertEqual(restored["integrity"], "ok")
            self.assertIsNotNone(goal)
            self.assertEqual([item["event_type"] for item in events], ["backup.ready"])
            self.assertEqual(len(outbox), 1)

    async def test_manifest_checksum_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            live_path = root / "live.db"
            backup_path = root / "backup.db"
            store = OPCStore(live_path)
            await store.initialize()
            try:
                await OperationsBackupManager(live_path).create_backup(backup_path)
            finally:
                await store.close()
            with backup_path.open("ab") as handle:
                handle.write(b"tampered")

            with self.assertRaisesRegex(OperationsBackupError, "checksum"):
                await OperationsBackupManager.inspect_backup(backup_path)

    async def test_restore_requires_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            live_path = root / "live.db"
            backup_path = root / "backup.db"
            destination = root / "destination.db"
            store = OPCStore(live_path)
            await store.initialize()
            try:
                await OperationsBackupManager(live_path).create_backup(backup_path)
            finally:
                await store.close()
            destination.write_bytes(b"do not replace implicitly")

            with self.assertRaises(FileExistsError):
                await OperationsBackupManager.restore_backup(backup_path, destination)


if __name__ == "__main__":
    unittest.main()
