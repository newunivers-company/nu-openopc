from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from opc.core.config import OPCConfig
from opc.core.models import DelegationWorkItem, Phase
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
