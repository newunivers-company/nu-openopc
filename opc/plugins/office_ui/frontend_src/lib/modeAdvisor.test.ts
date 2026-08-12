import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { assessExecutionMode, type ModeAssessmentInput } from './modeAdvisor'

const boundedTask: ModeAssessmentInput = {
  deliverableCount: 1,
  roleCount: 1,
  independentWorkstreams: 1,
  dependencyCount: 0,
  boundedScope: true,
  inputComplete: true,
  requirementsStable: true,
  independentReviewRequired: false,
  finalIntegrationRequired: false,
  riskLevel: 'low',
  estimatedDurationMinutes: 20,
}

const taskResult = assessExecutionMode(boundedTask)
assert.equal(taskResult.recommendation, 'task')
assert.equal(taskResult.companyBenefitScore, 0)
assert.equal(taskResult.advisoryOnly, true)

const companyResult = assessExecutionMode({
  ...boundedTask,
  deliverableCount: 4,
  roleCount: 3,
  independentWorkstreams: 3,
  dependencyCount: 2,
  boundedScope: false,
  independentReviewRequired: true,
  finalIntegrationRequired: true,
  riskLevel: 'high',
})
assert.equal(companyResult.recommendation, 'company')
assert.equal(companyResult.companyBenefitScore, 10)
assert.ok(companyResult.factors.some(item => item.code === 'independent_review'))

const clarifyResult = assessExecutionMode({
  ...boundedTask,
  inputComplete: false,
  requirementsStable: false,
})
assert.equal(clarifyResult.recommendation, 'clarify')
assert.equal(clarifyResult.blockers.length, 2)

interface ParityCase {
  name: string
  request: {
    deliverable_count: number
    role_count: number
    independent_workstreams: number
    dependency_count: number
    bounded_scope: boolean
    input_complete: boolean
    requirements_stable: boolean
    independent_review_required: boolean
    final_integration_required: boolean
    risk_level: 'low' | 'medium' | 'high'
    estimated_duration_minutes?: number
    workload_key?: string
    outcome_observations?: Array<{
      workload_key: string
      authority: 'human_confirmed' | 'independent_judge' | 'llm_draft' | 'simulation'
      task_quality: number
      company_quality: number
      task_duration_seconds: number
      company_duration_seconds: number
      task_external_calls: number
      company_external_calls: number
    }>
  }
  expected: {
    recommendation: string
    company_benefit_score: number
    evidence_veto: boolean
  }
}

const parityCases = JSON.parse(readFileSync(
  new URL('../../../../../tests/fixtures/mode_advisor_parity.json', import.meta.url),
  'utf8',
)) as ParityCase[]
for (const item of parityCases) {
  const request = item.request
  const result = assessExecutionMode({
    deliverableCount: request.deliverable_count,
    roleCount: request.role_count,
    independentWorkstreams: request.independent_workstreams,
    dependencyCount: request.dependency_count,
    boundedScope: request.bounded_scope,
    inputComplete: request.input_complete,
    requirementsStable: request.requirements_stable,
    independentReviewRequired: request.independent_review_required,
    finalIntegrationRequired: request.final_integration_required,
    riskLevel: request.risk_level,
    estimatedDurationMinutes: request.estimated_duration_minutes,
    workloadKey: request.workload_key,
    outcomeObservations: request.outcome_observations?.map(observation => ({
      workloadKey: observation.workload_key,
      authority: observation.authority,
      taskQuality: observation.task_quality,
      companyQuality: observation.company_quality,
      taskDurationSeconds: observation.task_duration_seconds,
      companyDurationSeconds: observation.company_duration_seconds,
      taskExternalCalls: observation.task_external_calls,
      companyExternalCalls: observation.company_external_calls,
    })),
  })
  assert.equal(result.recommendation, item.expected.recommendation, item.name)
  assert.equal(result.companyBenefitScore, item.expected.company_benefit_score, item.name)
  assert.equal(result.evidenceVeto, item.expected.evidence_veto, item.name)
}

console.log('modeAdvisor.test.ts: OK (mode recommendations are deterministic and advisory)')
