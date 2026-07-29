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
    OperatorAction,
    ResourceBudget,
    RoleOutcome,
    RunManifest,
    RunMetrics,
    RunScorecard,
    RunStatus,
    StaffingCandidate,
    StaffingDecision,
)
from opc.operations.campaign_runner import (
    CampaignBudget,
    CampaignRunner,
    CampaignSlotRunner,
    SlotExecution,
    SubprocessSlotExecutor,
)
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.canary import ProviderCanaryService
from opc.operations.durable import DurableRunKernel
from opc.operations.evaluation import OutcomeEvaluator
from opc.operations.learning import LearningAssetManager
from opc.operations.learning_activation import LearningActivationResolver
from opc.operations.learning_experiments import (
    build_release_playbook_experiment,
    verify_release_playbook_experiment,
)
from opc.operations.mission_control import MissionControlService
from opc.operations.mode_advisor import (
    ModeAssessmentRequest,
    assess_execution_mode,
)
from opc.operations.operator_actions import OperatorActionService
from opc.operations.promotion import build_promotion_dossier
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
from opc.operations.skill_assembly import SkillAssemblyService

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
    "OperatorAction",
    "ResourceBudget",
    "RoleOutcome",
    "RunManifest",
    "RunMetrics",
    "RunScorecard",
    "RunStatus",
    "StaffingCandidate",
    "StaffingDecision",
    "CampaignBudget",
    "CampaignRunner",
    "CampaignSlotRunner",
    "SlotExecution",
    "SubprocessSlotExecutor",
    "DurableRunKernel",
    "LearningAssetManager",
    "LearningActivationResolver",
    "build_release_playbook_experiment",
    "verify_release_playbook_experiment",
    "MissionControlService",
    "ModeAssessmentRequest",
    "assess_execution_mode",
    "OperatorActionService",
    "build_promotion_dossier",
    "OperationsRepository",
    "OperationsService",
    "OutcomeEvaluator",
    "StaffingOptimizer",
    "SkillAssemblyService",
    "UnifiedCapabilityBroker",
    "ProviderCanaryService",
    "ApprovedResourcePipeline",
    "ResourceApprovalTokenIssuer",
    "ResourcePipelineRequest",
    "ResourcePipelineResult",
    "RoutingOutcomeService",
]
