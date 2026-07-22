"""Outcome-driven operating kernel for OpenOPC.

The package closes the loop between declared goals, durable execution,
measured outcomes, learned operating assets, and the secretary-facing mission
control view.  It intentionally depends on the existing project-scoped
``OPCStore`` instead of introducing a second database.
"""

from opc.operations.models import (
    AcceptanceCriterion,
    CapabilityKind,
    CapabilityRequest,
    CapabilityRoute,
    ProviderCanaryResult,
    ProviderUsageEvent,
    RouteExecutionContract,
    GoalContract,
    GateStatus,
    LearningAsset,
    LearningAssetEvaluation,
    LearningAssetStatus,
    ResourceBudget,
    RoleOutcome,
    RunManifest,
    RunMetrics,
    RunScorecard,
    RunStatus,
    StaffingCandidate,
    StaffingDecision,
)
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.canary import ProviderCanaryService
from opc.operations.durable import DurableRunKernel
from opc.operations.evaluation import OutcomeEvaluator
from opc.operations.learning import LearningAssetManager
from opc.operations.mission_control import MissionControlService
from opc.operations.repository import OperationsRepository
from opc.operations.resource_pipeline import (
    ApprovedResourcePipeline,
    ResourceApprovalTokenIssuer,
    ResourcePipelineRequest,
    ResourcePipelineResult,
)
from opc.operations.routing_outcomes import RoutingOutcomeService
from opc.operations.service import OperationsService
from opc.operations.staffing import StaffingOptimizer

__all__ = [
    "AcceptanceCriterion",
    "CapabilityKind",
    "CapabilityRequest",
    "CapabilityRoute",
    "ProviderCanaryResult",
    "ProviderUsageEvent",
    "RouteExecutionContract",
    "GoalContract",
    "GateStatus",
    "LearningAsset",
    "LearningAssetEvaluation",
    "LearningAssetStatus",
    "ResourceBudget",
    "RoleOutcome",
    "RunManifest",
    "RunMetrics",
    "RunScorecard",
    "RunStatus",
    "StaffingCandidate",
    "StaffingDecision",
    "DurableRunKernel",
    "LearningAssetManager",
    "MissionControlService",
    "OperationsRepository",
    "OperationsService",
    "OutcomeEvaluator",
    "StaffingOptimizer",
    "UnifiedCapabilityBroker",
    "ProviderCanaryService",
    "ApprovedResourcePipeline",
    "ResourceApprovalTokenIssuer",
    "ResourcePipelineRequest",
    "ResourcePipelineResult",
    "RoutingOutcomeService",
]
