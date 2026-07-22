from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.core.config import OPCConfig
from opc.database.store import OPCStore
from opc.engine import OPCEngine


class OperationsEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_binds_operations_to_tools_recruiter_and_secretary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = OPCStore(root / ".opc" / "projects" / "demo" / "tasks.db")
            await store.initialize()
            engine = OPCEngine(
                OPCConfig(),
                opc_home=root / ".opc",
                project_id="demo",
                store=store,
                owns_store=False,
                run_startup_reconcile=False,
            )
            try:
                await engine.initialize()
                self.assertIsNotNone(engine.operations)
                assert engine.operations is not None
                self.assertIs(engine.company_recruiter.staffing_optimizer, engine.operations.staffing)
                self.assertIs(engine.secretary.mission_control, engine.operations.mission_control)
                self.assertIs(engine.operations.capabilities.adapter_registry, engine.adapter_registry)
                tool_names = {item.name for item in engine.tool_registry.list_tools()}
                self.assertTrue(
                    {
                        "operations_mission_control",
                        "operations_capability_plan",
                        "operations_active_learning",
                    }.issubset(tool_names)
                )
                result = await engine.tool_registry.invoke(
                    "operations_mission_control",
                    {"project_id": "demo"},
                )
                self.assertTrue(result["success"])
                self.assertEqual(result["result"]["project_id"], "demo")
                rejected = await engine.tool_registry.invoke(
                    "operations_mission_control",
                    {"project_id": "another-project"},
                )
                self.assertFalse(rejected["success"])
                self.assertIn("cross-project", rejected["error"])
            finally:
                await engine.shutdown()
                await store.close()

    async def test_disabled_operations_do_not_wire_runtime_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = OPCStore(root / ".opc" / "projects" / "demo" / "tasks.db")
            await store.initialize()
            config = OPCConfig()
            config.system.operations.enabled = False
            engine = OPCEngine(
                config,
                opc_home=root / ".opc",
                project_id="demo",
                store=store,
                owns_store=False,
                run_startup_reconcile=False,
            )
            try:
                await engine.initialize()
                self.assertIsNone(engine.operations)
                self.assertIsNone(engine.company_recruiter.staffing_optimizer)
                self.assertIsNone(engine.secretary.mission_control)
                tool_names = {item.name for item in engine.tool_registry.list_tools()}
                self.assertFalse(any(name.startswith("operations_") for name in tool_names))
            finally:
                await engine.shutdown()
                await store.close()


if __name__ == "__main__":
    unittest.main()
