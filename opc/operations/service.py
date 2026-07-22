"""Facade that binds every operations subsystem to one project store."""

from __future__ import annotations

from typing import Any

from opc.core.config import OperationsConfig
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.durable import DurableRunKernel
from opc.operations.evaluation import OutcomeEvaluator
from opc.operations.learning import LearningAssetManager
from opc.operations.mission_control import MissionControlService
from opc.operations.repository import OperationsRepository
from opc.operations.staffing import StaffingOptimizer


class OperationsService:
    """One bound goal-to-learning operating loop for an ``OPCStore``."""

    def __init__(
        self,
        store: Any,
        config: OperationsConfig | None = None,
        *,
        llm_router: Any | None = None,
        resource_bridge: Any | None = None,
        adapter_registry: Any | None = None,
        default_llm_model: str = "",
        default_llm_api_base: str = "",
    ) -> None:
        self.config = config or OperationsConfig()
        self.repository = OperationsRepository(store)
        self.evaluator = OutcomeEvaluator(self.repository, self.config.evaluation)
        self.durable = DurableRunKernel(self.repository, self.config.durable)
        self.learning = LearningAssetManager(self.repository, self.config.learning)
        self.capabilities = UnifiedCapabilityBroker(
            self.repository,
            llm_router=llm_router,
            resource_bridge=resource_bridge,
            adapter_registry=adapter_registry,
            default_llm_model=default_llm_model,
            default_llm_api_base=default_llm_api_base,
        )
        self.staffing = StaffingOptimizer(self.repository, self.config.staffing)
        self.mission_control = MissionControlService(self.repository, self.durable)

    def rebind(self, store: Any) -> None:
        self.repository.rebind(store)

    def bind_adapter_registry(self, registry: Any | None) -> None:
        self.capabilities.bind_adapter_registry(registry)
