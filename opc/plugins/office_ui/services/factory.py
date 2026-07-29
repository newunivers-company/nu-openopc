"""Factory for shared Office services."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Awaitable, Callable

import aiosqlite

from opc.core.config import OPCConfig, get_opc_home
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.plugins.office_ui.agent_store import AgentStore
from opc.plugins.office_ui.chat_store import ChatStore
from opc.plugins.office_ui.event_adapter import EventAdapter

from . import OfficeServices
from .context import ModeState, OfficeServiceContext


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
            config_dir = get_opc_home() / "config"
            self.config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
        self.engine = self._provided_engine or OPCEngine(
            config=self.config,
            project_id=self.project_id,
            on_progress=self.on_progress,
            on_runtime_event=self.on_runtime_event,
            on_escalation=self.on_escalation,
        )
        opc_home = getattr(self.engine, "opc_home", get_opc_home())
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
        context = OfficeServiceContext(
            engine=self.engine,
            agent_store=agent_store,
            chat_store=chat_store,
            event_adapter=event_adapter,
            mode_state=mode_state,
        )
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
