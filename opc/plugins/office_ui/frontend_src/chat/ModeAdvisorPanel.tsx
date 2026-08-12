import { useMemo, useState } from 'react'
import {
  assessExecutionMode,
  type ExecutionModeRecommendation,
  type ModeAssessmentInput,
  type ModeRiskLevel,
} from '../lib/modeAdvisor'

const DEFAULT_INPUT: ModeAssessmentInput = {
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
}

function recommendationLabel(value: ExecutionModeRecommendation): string {
  if (value === 'company') return 'Company Mode'
  if (value === 'task') return 'Task Mode'
  return 'Clarify first'
}

export function ModeAdvisorPanel({
  disabled,
  currentMode,
  onApply,
}: {
  disabled?: boolean
  currentMode: 'task' | 'company'
  onApply: (mode: 'task' | 'company') => void
}) {
  const [input, setInput] = useState(DEFAULT_INPUT)
  const result = useMemo(() => assessExecutionMode(input), [input])
  const updateCount = (
    key: 'deliverableCount' | 'roleCount' | 'independentWorkstreams' | 'dependencyCount',
    rawValue: string,
  ) => {
    const minimum = key === 'dependencyCount' ? 0 : 1
    const value = Math.max(minimum, Number.parseInt(rawValue, 10) || minimum)
    setInput(previous => ({ ...previous, [key]: value }))
  }
  const updateFlag = (
    key:
      | 'boundedScope'
      | 'inputComplete'
      | 'requirementsStable'
      | 'independentReviewRequired'
      | 'finalIntegrationRequired',
    value: boolean,
  ) => setInput(previous => ({ ...previous, [key]: value }))

  return (
    <details className="composer-mode-advisor">
      <summary aria-label="Assess Task or Company Mode fit">Assess fit</summary>
      <div className="composer-mode-advisor-panel">
        <header>
          <div>
            <span>Deterministic mode check</span>
            <strong>{recommendationLabel(result.recommendation)}</strong>
          </div>
          <span className={`composer-mode-advisor-score is-${result.recommendation}`}>
            {result.companyBenefitScore} / 10
          </span>
        </header>

        <div className="composer-mode-advisor-counts">
          <label>
            Deliverables
            <input
              type="number"
              min={1}
              value={input.deliverableCount}
              onChange={event => updateCount('deliverableCount', event.target.value)}
            />
          </label>
          <label>
            Roles
            <input
              type="number"
              min={1}
              value={input.roleCount}
              onChange={event => updateCount('roleCount', event.target.value)}
            />
          </label>
          <label>
            Workstreams
            <input
              type="number"
              min={1}
              value={input.independentWorkstreams}
              onChange={event => updateCount('independentWorkstreams', event.target.value)}
            />
          </label>
          <label>
            Dependencies
            <input
              type="number"
              min={0}
              value={input.dependencyCount}
              onChange={event => updateCount('dependencyCount', event.target.value)}
            />
          </label>
        </div>

        <div className="composer-mode-advisor-options">
          <label>
            Risk
            <select
              value={input.riskLevel}
              onChange={event => setInput(previous => ({
                ...previous,
                riskLevel: event.target.value as ModeRiskLevel,
              }))}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <label>
            Duration, min
            <input
              type="number"
              min={1}
              placeholder="Unknown"
              value={input.estimatedDurationMinutes ?? ''}
              onChange={event => setInput(previous => ({
                ...previous,
                estimatedDurationMinutes: event.target.value
                  ? Math.max(1, Number.parseInt(event.target.value, 10) || 1)
                  : undefined,
              }))}
            />
          </label>
        </div>

        <div className="composer-mode-advisor-flags">
          <label>
            <input
              type="checkbox"
              checked={input.boundedScope}
              onChange={event => updateFlag('boundedScope', event.target.checked)}
            />
            Bounded scope
          </label>
          <label>
            <input
              type="checkbox"
              checked={input.inputComplete}
              onChange={event => updateFlag('inputComplete', event.target.checked)}
            />
            Inputs complete
          </label>
          <label>
            <input
              type="checkbox"
              checked={input.requirementsStable}
              onChange={event => updateFlag('requirementsStable', event.target.checked)}
            />
            Criteria stable
          </label>
          <label>
            <input
              type="checkbox"
              checked={input.independentReviewRequired}
              onChange={event => updateFlag('independentReviewRequired', event.target.checked)}
            />
            Independent review
          </label>
          <label>
            <input
              type="checkbox"
              checked={input.finalIntegrationRequired}
              onChange={event => updateFlag('finalIntegrationRequired', event.target.checked)}
            />
            Final integration
          </label>
        </div>

        <p>{result.summary}</p>
        {result.observedEvidence.matchedTrustedPairs > 0 && (
          <p>
            {result.observedEvidence.matchedTrustedPairs} trusted pair(s) · quality delta{' '}
            {result.observedEvidence.meanQualityDelta >= 0 ? '+' : ''}
            {result.observedEvidence.meanQualityDelta.toFixed(2)} · duration{' '}
            {result.observedEvidence.meanDurationRatio?.toFixed(1)}× · calls{' '}
            {result.observedEvidence.meanExternalCallRatio?.toFixed(1)}×
          </p>
        )}
        {result.factors.length > 0 && (
          <ul>
            {result.factors.slice(0, 3).map(item => (
              <li key={item.code}>{item.weight > 0 ? '+' : ''}{item.weight} · {item.message}</li>
            ))}
          </ul>
        )}
        <footer>
          <span>Advisory only · threshold {result.decisionThreshold}</span>
          {result.recommendation !== 'clarify' && (
            <button
              type="button"
              disabled={disabled || currentMode === result.recommendation}
              onClick={() => onApply(result.recommendation as 'task' | 'company')}
            >
              {currentMode === result.recommendation
                ? 'Already selected'
                : `Use ${recommendationLabel(result.recommendation)}`}
            </button>
          )}
        </footer>
      </div>
    </details>
  )
}
