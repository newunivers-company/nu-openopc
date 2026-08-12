"""Factory for shared Office services."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Awaitable, Callable

import aiosqlite

from opc.core import config as core_config
from opc.core.config import OPCConfig
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.plugins.office_ui.agent_store import AgentStore
from opc.plugins.office_ui.chat_store import ChatStore
from opc.plugins.office_ui.event_adapter import EventAdapter

from . import OfficeServices
from .context import ModeState, OfficeServiceContext

_ACTIVE_SAVED_ORG_STATE_KEY = "active_saved_org"


class OfficeServiceFactory:
    """Async context manager that owns engine and UI-state persistence."""

    def __init__(
        self,
        *,
        config: OPCConfig | None = None,
        engine: OPCEngine | None = None,
        project_id: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_runtime_event: Callable[[Any], Awaitable[None]] | None = None,
        on_escalation: Callable[..., Awaitable[str | None]] | None = None,
        read_only: bool = False,
    ) -> None:
        self.config = config
        self._provided_engine = engine
        self._owns_engine = engine is None
        self._opc_home = core_config.get_opc_home()
        self.project_id = project_id
        self.on_progress = on_progress
        self.on_runtime_event = on_runtime_event
        self.on_escalation = on_escalation
        self.read_only = bool(read_only)
        self.db: aiosqlite.Connection | None = None
        self._read_only_store: OPCStore | None = None
        self.engine: OPCEngine | None = None
        self.services: OfficeServices | None = None

    async def __aenter__(self) -> OfficeServices:
        if self.config is None:
            config_dir = core_config.get_opc_home() / "config"
            self.config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
        self.engine = self._provided_engine or OPCEngine(
            config=self.config,
            opc_home=self._opc_home,
            project_id=self.project_id,
            on_progress=self.on_progress,
            on_runtime_event=self.on_runtime_event,
            on_escalation=self.on_escalation,
        )
        opc_home = getattr(self.engine, "opc_home", self._opc_home)
        opc_home.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(str(opc_home / "ui_state.db"))
        # Wait for a concurrent writer (e.g. a running office-UI server)
        # instead of failing after sqlite's 5s default with 'database is locked'.
        await self.db.execute("PRAGMA busy_timeout=30000")
        agent_store = AgentStore(self.db)
        await agent_store.initialize()
        chat_store = ChatStore(self.db)
        await chat_store.initialize()
        event_adapter = EventAdapter()
        if self._owns_engine and self.read_only:
            normalized_project = str(self.project_id or "").strip()
            store_path = (
                opc_home / "projects" / normalized_project / "tasks.db"
                if normalized_project
                else opc_home / "global.db"
            )
            self._read_only_store = OPCStore(store_path)
            await self._read_only_store.initialize(
                run_startup_maintenance=False
            )
            self.engine.store = self._read_only_store
        elif self._owns_engine:
            await self.engine.initialize()
        mode_state = ModeState(
            exec_mode=await agent_store.get_server_state("exec_mode", "task"),
            company_profile=await agent_store.get_server_state("company_profile", "corporate"),
            task_preferred_agent=await agent_store.get_server_state("task_preferred_agent", "native"),
        )
        if mode_state.exec_mode in {"org", "custom"}:
            try:
                from opc.core.org_config import apply_org_config_payload_to_config, load_org_config_payload

                payload, source_path = load_org_config_payload(opc_home / "config")
                self.config = apply_org_config_payload_to_config(self.config, payload, source_path=source_path)
                self.engine.config = self.config
                org_engine = getattr(self.engine, "org_engine", None)
                if org_engine is not None:
                    org_engine.config = self.config
                    org_engine.reload_from_config()
                talent_market = getattr(self.engine, "talent_market", None)
                if talent_market is not None:
                    talent_market.config = self.config
            except (FileNotFoundError, ValueError):
                # Keep the factory usable so the caller can repair or select a
                # valid organization; mutating services still reject Corporate.
                mode_state.exec_mode = "task"
                mode_state.company_profile = "corporate"
        context = OfficeServiceContext(
            engine=self.engine,
            agent_store=agent_store,
            chat_store=chat_store,
            event_adapter=event_adapter,
            mode_state=mode_state,
        )

        async def get_active_saved_org_name() -> str:
            from opc.core.org_config import (
                org_config_path,
                read_org_index,
                validate_saved_org_id,
                write_org_index,
            )

            config_dir = opc_home / "config"
            try:
                active_id = read_org_index(config_dir)
                if active_id and org_config_path(config_dir, active_id).exists():
                    return active_id
            except (OSError, TypeError, ValueError):
                pass
            legacy = await agent_store.get_server_state(
                _ACTIVE_SAVED_ORG_STATE_KEY,
                "",
            )
            try:
                active_id = validate_saved_org_id(legacy)
            except ValueError:
                return ""
            if not org_config_path(config_dir, active_id).exists():
                return ""
            write_org_index(config_dir, active_id)
            return active_id

        async def set_active_saved_org_name(organization_id: str) -> None:
            from opc.core.org_config import validate_saved_org_id, write_org_index

            active_id = validate_saved_org_id(organization_id)
            write_org_index(opc_home / "config", active_id)
            await agent_store.set_server_state(
                _ACTIVE_SAVED_ORG_STATE_KEY,
                active_id,
            )

        context.get_active_saved_org_name = get_active_saved_org_name
        context.set_active_saved_org_name = set_active_saved_org_name
        if self._provided_engine is not None:
            # Embedded callers (notably the CLI board) already own the exact
            # project engine and its in-memory/test store.  Re-resolving it
            # would either create a duplicate engine or lose that store.
            context.project_engine_resolver = lambda _project_id: self.engine
        self.services = OfficeServices(context)
        return self.services

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._read_only_store is not None:
            await self._read_only_store.close()
        elif self._owns_engine and self.engine is not None:
            await self.engine.shutdown()
        if self.db is not None:
            await self.db.close()
