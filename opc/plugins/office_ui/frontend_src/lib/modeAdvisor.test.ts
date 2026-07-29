import assert from 'node:assert/strict'
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

console.log('modeAdvisor.test.ts: OK (mode recommendations are deterministic and advisory)')
