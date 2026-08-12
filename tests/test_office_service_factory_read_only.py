from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from opc.core.config import OPCConfig
from opc.core.models import DelegationWorkItem, Phase
from opc.core.org_config import read_org_index, write_org_index
from opc.database.store import OPCStore
from opc.plugins.office_ui.services.factory import OfficeServiceFactory


@pytest.mark.asyncio
async def test_read_only_factory_does_not_sweep_live_work_item_claims() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        opc_home = Path(tmpdir) / ".opc"
        project_id = "active-company"
        db_path = opc_home / "projects" / project_id / "tasks.db"
        store = OPCStore(db_path)
        await store.initialize()
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="work-live",
                run_id="run-live",
                cell_id="cell-live",
                role_id="engineer",
                seat_id="seat::engineer",
                title="External execution in progress",
                phase=Phase.RUNNING,
                claimed_by_role_runtime_session_id="role-session-live",
                claimed_by_seat_id="seat::engineer",
            )
        )
        await store.close()

        with patch.dict(os.environ, {"OPC_HOME": str(opc_home)}):
            async with OfficeServiceFactory(
                config=OPCConfig(),
                project_id=project_id,
                read_only=True,
            ) as services:
                result = await services.runtime.checkpoints(
                    project_id=project_id
                )
                assert result.payload["project_id"] == project_id
                assert services.context.engine._initialized is False

        inspection = OPCStore(db_path)
        await inspection.initialize(run_startup_maintenance=False)
        item = await inspection.get_delegation_work_item("work-live")
        await inspection.close()

        assert item is not None
        assert (
            item.claimed_by_role_runtime_session_id
            == "role-session-live"
        )
        assert item.claimed_by_seat_id == "seat::engineer"


@pytest.mark.asyncio
async def test_factory_shared_services_read_and_persist_active_saved_org() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        opc_home = Path(tmpdir) / ".opc"
        config_dir = opc_home / "config"
        org_dir = config_dir / "company_orgs"
        org_dir.mkdir(parents=True)
        (org_dir / "org_lab_config.yaml").write_text("organization_id: lab\n")
        (org_dir / "org_studio_config.yaml").write_text("organization_id: studio\n")
        write_org_index(config_dir, "lab")
        project_store = OPCStore(opc_home / "projects" / "default" / "tasks.db")
        await project_store.initialize()
        await project_store.close()

        with patch.dict(os.environ, {"OPC_HOME": str(opc_home)}):
            async with OfficeServiceFactory(
                config=OPCConfig(),
                project_id="default",
                read_only=True,
            ) as services:
                getter = services.context.get_active_saved_org_name
                setter = services.context.set_active_saved_org_name
                assert getter is not None
                assert setter is not None
                assert await getter() == "lab"
                await setter("studio")
                assert await getter() == "studio"

        assert read_org_index(config_dir) == "studio"


@pytest.mark.asyncio
async def test_factory_resolves_opc_home_at_context_entry_time() -> None:
    """Keep test/runtime overrides valid after the factory module is imported."""
    with tempfile.TemporaryDirectory() as tmpdir:
        opc_home = Path(tmpdir) / ".opc"
        with patch("opc.core.config.get_opc_home", return_value=opc_home):
            async with OfficeServiceFactory(
                config=OPCConfig(),
                project_id="late-bound",
            ):
                pass

        assert (opc_home / "ui_state.db").is_file()
        assert (opc_home / "projects" / "late-bound" / "tasks.db").is_file()
