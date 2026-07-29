export type ExecutionModeRecommendation = 'task' | 'company' | 'clarify'
export type ModeRiskLevel = 'low' | 'medium' | 'high'

export interface ModeAssessmentInput {
  deliverableCount: number
  roleCount: number
  independentWorkstreams: number
  dependencyCount: number
  boundedScope: boolean
  inputComplete: boolean
  requirementsStable: boolean
  independentReviewRequired: boolean
  finalIntegrationRequired: boolean
  riskLevel: ModeRiskLevel
  estimatedDurationMinutes?: number
}

export interface ModeAssessmentFactor {
  code: string
  weight: number
  message: string
}

export interface ModeAssessment {
  recommendation: ExecutionModeRecommendation
  confidence: 'high' | 'medium'
  advisoryOnly: true
  companyBenefitScore: number
  decisionThreshold: 5
  blockers: string[]
  factors: ModeAssessmentFactor[]
  summary: string
}

export function assessExecutionMode(input: ModeAssessmentInput): ModeAssessment {
  const blockers: string[] = []
  if (!input.inputComplete) {
    blockers.push('Required source material or fixture evidence is incomplete.')
  }
  if (!input.requirementsStable) {
    blockers.push('Acceptance boundaries are not stable enough to assign safely.')
  }

  const factors: ModeAssessmentFactor[] = []
  const factor = (code: string, weight: number, message: string) => {
    factors.push({ code, weight, message })
  }
  if (input.independentWorkstreams >= 2) {
    factor(
      'parallel_workstreams',
      input.independentWorkstreams >= 3 ? 3 : 2,
      `${input.independentWorkstreams} independent workstreams can run in parallel.`,
    )
  }
  if (input.roleCount >= 2) {
    factor(
      'specialized_roles',
      input.roleCount >= 3 ? 2 : 1,
      `${input.roleCount} distinct accountable roles are expected.`,
    )
  }
  if (input.independentReviewRequired) {
    factor(
      'independent_review',
      2,
      'The deliverable requires review independent from its executor.',
    )
  }
  if (input.finalIntegrationRequired) {
    factor(
      'final_integration',
      2,
      'Multiple outputs require one accountable final integrator.',
    )
  }
  if (input.dependencyCount >= 2) {
    factor(
      'dependency_graph',
      1,
      `${input.dependencyCount} dependencies require ordered handoffs.`,
    )
  }
  if (input.deliverableCount >= 3) {
    factor(
      'multiple_deliverables',
      1,
      `${input.deliverableCount} deliverables increase coordination value.`,
    )
  }
  if (input.riskLevel === 'high') {
    factor(
      'high_risk',
      1,
      'High-risk work benefits from explicit ownership and review gates.',
    )
  }
  if (
    input.boundedScope
    && input.roleCount === 1
    && input.independentWorkstreams === 1
  ) {
    factor(
      'bounded_single_owner',
      -3,
      'The scope is bounded and has one natural execution owner.',
    )
  }
  if (
    input.estimatedDurationMinutes != null
    && input.estimatedDurationMinutes <= 30
    && input.deliverableCount === 1
  ) {
    factor(
      'short_direct_path',
      -1,
      'The estimated duration and single deliverable favor a direct path.',
    )
  }

  const companyBenefitScore = Math.max(
    0,
    Math.min(10, factors.reduce((total, item) => total + item.weight, 0)),
  )
  let recommendation: ExecutionModeRecommendation
  let confidence: 'high' | 'medium'
  if (blockers.length > 0) {
    recommendation = 'clarify'
    confidence = 'high'
  } else if (companyBenefitScore >= 5) {
    recommendation = 'company'
    confidence = companyBenefitScore >= 7 ? 'high' : 'medium'
  } else {
    recommendation = 'task'
    confidence = companyBenefitScore <= 2 ? 'high' : 'medium'
  }

  const summary = recommendation === 'clarify'
    ? `Do not start until the input and acceptance contract is complete (${blockers.length} blocker(s)).`
    : recommendation === 'company'
      ? 'Coordination, independent ownership, review, or integration is expected to add material value.'
      : 'One execution owner can take the bounded work directly without paying a coordination tax.'
  return {
    recommendation,
    confidence,
    advisoryOnly: true,
    companyBenefitScore,
    decisionThreshold: 5,
    blockers,
    factors,
    summary,
  }
}
