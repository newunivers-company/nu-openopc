export type ExecutionModeRecommendation = 'task' | 'company' | 'clarify'
export type ModeRiskLevel = 'low' | 'medium' | 'high'

export interface ModeOutcomeObservation {
  workloadKey: string
  authority: 'human_confirmed' | 'independent_judge' | 'llm_draft' | 'simulation'
  taskQuality: number
  companyQuality: number
  taskDurationSeconds: number
  companyDurationSeconds: number
  taskExternalCalls: number
  companyExternalCalls: number
}

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
  workloadKey?: string
  outcomeObservations?: ModeOutcomeObservation[]
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
  structuralCompanyBenefitScore: number
  decisionThreshold: 5
  evidenceVeto: boolean
  observedEvidence: {
    workloadKey: string
    matchedTrustedPairs: number
    ignoredObservations: number
    confidence: 'none' | 'low' | 'medium' | 'high'
    meanQualityDelta: number
    meanDurationRatio?: number
    meanExternalCallRatio?: number
    withinProvisionalBudget: boolean
  }
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

  const structuralCompanyBenefitScore = Math.max(
    0,
    Math.min(10, factors.reduce((total, item) => total + item.weight, 0)),
  )
  const trustedAuthorities = new Set(['human_confirmed', 'independent_judge'])
  const workloadKey = (input.workloadKey ?? '').trim().toLowerCase()
  const suppliedObservations = input.outcomeObservations ?? []
  const observations = suppliedObservations.filter(item => (
    trustedAuthorities.has(item.authority)
    && item.workloadKey.trim().toLowerCase() === workloadKey
  ))
  const mean = (values: number[]): number => (
    values.reduce((total, value) => total + value, 0) / Math.max(1, values.length)
  )
  const meanQualityDelta = observations.length > 0
    ? mean(observations.map(item => item.companyQuality - item.taskQuality))
    : 0
  const meanDurationRatio = observations.length > 0
    ? mean(observations.map(item => item.companyDurationSeconds / item.taskDurationSeconds))
    : undefined
  const meanExternalCallRatio = observations.length > 0
    ? mean(observations.map(item => item.companyExternalCalls / Math.max(1, item.taskExternalCalls)))
    : undefined
  const withinProvisionalBudget = (
    meanDurationRatio != null
    && meanExternalCallRatio != null
    && meanDurationRatio <= 3
    && meanExternalCallRatio <= 8
  )
  let evidenceVeto = false
  if (observations.length > 0) {
    if (meanQualityDelta < 0 || (!withinProvisionalBudget && meanQualityDelta < 0.03)) {
      factor(
        'observed_company_underperformance',
        -4,
        'Trusted matched outcomes show Company Mode does not repay its observed coordination cost for this workload.',
      )
      evidenceVeto = true
    } else if (meanQualityDelta >= 0.03 && withinProvisionalBudget) {
      factor(
        'observed_company_lift',
        2,
        'Trusted matched outcomes show material Company quality lift within budget.',
      )
    } else if (withinProvisionalBudget) {
      factor(
        'observed_company_non_regression',
        1,
        'Trusted matched outcomes show Company non-regression within budget.',
      )
    }
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
  } else if (evidenceVeto) {
    recommendation = 'task'
    confidence = observations.length >= 3 ? 'high' : 'medium'
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
      : evidenceVeto
        ? 'Trusted matched outcomes show that the current Company topology does not repay its coordination cost.'
      : 'One execution owner can take the bounded work directly without paying a coordination tax.'
  return {
    recommendation,
    confidence,
    advisoryOnly: true,
    companyBenefitScore,
    structuralCompanyBenefitScore,
    decisionThreshold: 5,
    evidenceVeto,
    observedEvidence: {
      workloadKey,
      matchedTrustedPairs: observations.length,
      ignoredObservations: suppliedObservations.length - observations.length,
      confidence: observations.length >= 5
        ? 'high'
        : observations.length >= 3
          ? 'medium'
          : observations.length > 0
            ? 'low'
            : 'none',
      meanQualityDelta: Number(meanQualityDelta.toFixed(6)),
      meanDurationRatio: meanDurationRatio == null
        ? undefined
        : Number(meanDurationRatio.toFixed(6)),
      meanExternalCallRatio: meanExternalCallRatio == null
        ? undefined
        : Number(meanExternalCallRatio.toFixed(6)),
      withinProvisionalBudget,
    },
    blockers,
    factors,
    summary,
  }
}
