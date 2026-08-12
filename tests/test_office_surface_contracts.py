from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console
from typer.testing import CliRunner

from opc.cli.app import _InteractiveChatState, _handle_chat_slash_command, app
from opc.core.config import OPCConfig
from opc.plugins.office_ui.services.models import ServiceResult
from opc.plugins.office_ui.ws_handler import WSHandler


ORG_CREATE_PAYLOAD = {
    "ok": True,
    "name": "research_lab",
    "organization_id": "research_lab",
    "organization_name": "Research Lab",
    "filename": "org_research_lab_config.yaml",
    "roles_count": 2,
    "employees_count": 2,
}


class _FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _chat_state(tmp_path: Path) -> _InteractiveChatState:
    config = OPCConfig()
    config.org.organization_id = "research_lab"
    config.org.organization_name = "Research Lab"
    config.org.company_profile = "custom"
    engine = SimpleNamespace(
        config=config,
        opc_home=tmp_path / ".opc",
        project_id="default",
        org_engine=SimpleNamespace(config=config, reload_from_config=MagicMock()),
        talent_market=SimpleNamespace(config=config),
    )
    state = _InteractiveChatState(
        config=config,
        engine=engine,
        runtime_display=SimpleNamespace(),
        session_id="session-1",
        no_markdown=True,
    )
    state.mode = "org"
    state.company_profile = "custom"
    state.org_id = "research_lab"
    return state


def test_cli_org_create_routes_parsed_members_through_shared_service() -> None:
    captured: dict = {}
    org = SimpleNamespace(saved_create=AsyncMock(return_value=ServiceResult(dict(ORG_CREATE_PAYLOAD))))
    runtime = SimpleNamespace(mode_set=AsyncMock(return_value=ServiceResult({"mode": "org"})))

    async def fake_run(project, operation, **kwargs):
        result = await operation(SimpleNamespace(org=org, runtime=runtime))
        captured.update(result.payload)

    with patch("opc.cli.app._run_service_command", side_effect=fake_run):
        result = CliRunner().invoke(app, [
            "org", "saved", "create", "Research Lab",
            "--member", "Lead|Owns direction",
            "--member", "Analyst|Runs analysis|0",
            "--json",
        ])

    assert result.exit_code == 0, result.output
    assert captured == ORG_CREATE_PAYLOAD
    org.saved_create.assert_awaited_once_with(
        organization_name="Research Lab",
        members=[
            {"name": "Lead", "responsibility": "Owns direction"},
            {"name": "Analyst", "responsibility": "Runs analysis", "reports_to_index": 0},
        ],
    )
    runtime.mode_set.assert_awaited_once_with(
        mode="org", profile="custom", org_id="research_lab", sync_config=False,
    )


def test_websocket_org_create_forwards_shared_payload_unchanged() -> None:
    async def run() -> None:
        org = SimpleNamespace(saved_create=AsyncMock(return_value=ServiceResult(dict(ORG_CREATE_PAYLOAD))))
        handler = WSHandler.__new__(WSHandler)
        handler._ensure_office_services = MagicMock(return_value=SimpleNamespace(org=org))
        handler._apply_mode_switch = AsyncMock(return_value=True)
        handler._task_preferred_agent = "native"
        ws = _FakeWS()

        await handler._handle_org_saved_create(ws, {
            "organization_name": "Research Lab",
            "members": [{"name": "Lead"}, {"name": "Analyst", "reports_to_index": 0}],
        })

        assert ws.messages == [{"type": "org_saved_create", "payload": ORG_CREATE_PAYLOAD}]
        handler._apply_mode_switch.assert_awaited_once_with(
            "org", "custom", "native", org_id="research_lab",
        )

    asyncio.run(run())


def test_slash_org_create_uses_shared_service_contract(tmp_path: Path) -> None:
    console = Console(record=True, force_terminal=False, width=160)
    state = _chat_state(tmp_path)
    state.mode = "company"
    state.company_profile = "corporate"

    with patch(
        "opc.plugins.office_ui.services.org.OrgService.saved_create",
        new=AsyncMock(return_value=ServiceResult(dict(ORG_CREATE_PAYLOAD))),
    ) as saved_create, patch("opc.cli.app.console", console):
        asyncio.run(_handle_chat_slash_command(
            state,
            "/org saved create 'Research Lab' --member 'Lead|Owns direction' --member 'Analyst|Runs analysis|0'",
        ))

    saved_create.assert_awaited_once_with(
        organization_name="Research Lab",
        members=[
            {"name": "Lead", "responsibility": "Owns direction"},
            {"name": "Analyst", "responsibility": "Runs analysis", "reports_to_index": 0},
        ],
    )
    assert state.mode == "org"
    assert state.company_profile == "custom"
    assert state.org_id == "research_lab"
    assert "research_lab" in console.export_text()


def test_slash_market_preset_uses_shared_service_and_payload(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "action": "market_preset_applied",
        "package_id": "vc-investment-firm",
        "roles": 21,
        "work_item_templates": 23,
        "employees": 0,
        "persisted_employees": 0,
        "runtime_default_employees": 21,
    }
    console = Console(record=True, force_terminal=False, width=160)
    state = _chat_state(tmp_path)

    with patch(
        "opc.plugins.office_ui.services.market.MarketService.apply_preset",
        new=AsyncMock(return_value=ServiceResult(dict(payload))),
    ) as apply_preset, patch("opc.cli.app.console", console):
        asyncio.run(_handle_chat_slash_command(
            state,
            "/market apply-preset vc-investment-firm --strategy overwrite",
        ))

    apply_preset.assert_awaited_once_with(preset_id="vc-investment-firm", strategy="overwrite")
    rendered = console.export_text()
    for key in ("market_preset_applied", "vc-investment-firm", "21", "23"):
        assert key in rendered
