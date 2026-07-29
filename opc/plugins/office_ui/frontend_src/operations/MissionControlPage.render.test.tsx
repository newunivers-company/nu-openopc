/**
 * Behavior-level render tests for the Mission Control governed action flow.
 *
 * Unlike MissionControlPage.test.tsx (source-text assertions), these mount
 * the real component in jsdom and verify the two-phase contract the backend
 * relies on: plan → immutable digest review → digest-bound confirm with an
 * explicit operator identity.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { MissionControlPage } from './MissionControlPage'
import type {
  MissionActionPayload,
  MissionControlAlert,
  MissionControlPayload,
} from '../lib/wsClient'

const PLAN_DIGEST = 'd'.repeat(64)

const actionableAlert: MissionControlAlert = {
  severity: 'critical',
  kind: 'stalled_run',
  title: 'Run run-1 appears stalled',
  detail: 'No terminal event was recorded.',
  action: 'Recover the run.',
  run_id: 'run-1',
  action_kind: 'recover_run',
  action_target_id: 'run-1',
}

const snapshot: MissionControlPayload = {
  available: true,
  project_id: 'demo',
  alerts: [actionableAlert],
}

const plannedAction: MissionActionPayload = {
  ok: true,
  phase: 'plan',
  project_id: 'demo',
  action: {
    action_id: 'action-1',
    project_id: 'demo',
    kind: 'recover_run',
    target_id: 'run-1',
    status: 'planned',
    plan_digest: PLAN_DIGEST,
    consequence: 'Recovers run-1 and requeues its outbox work.',
    requires_confirmation: true,
    expires_at: '2026-07-28T12:00:00Z',
  },
}

interface Overrides {
  data?: MissionControlPayload | null
  loading?: boolean
  onRefresh?: () => void
  actionData?: MissionActionPayload | null
  actionLoading?: boolean
  onPlanAction?: (alert: MissionControlAlert) => void
  onExecuteAction?: (actionId: string, planDigest: string, operatorId: string) => void
}

function renderPage(overrides: Overrides = {}) {
  const props = {
    data: snapshot,
    loading: false,
    onRefresh: vi.fn(),
    actionData: null,
    actionLoading: false,
    onPlanAction: vi.fn(),
    onExecuteAction: vi.fn(),
    ...overrides,
  }
  return { ...render(<MissionControlPage {...props} />), props }
}

describe('MissionControlPage governed action flow', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('marks the first-load snapshot as busy', () => {
    const { container } = renderPage({ data: null, loading: true })
    expect(screen.getByText('Building the operations snapshot')).toBeInTheDocument()
    expect(container.querySelector('section[aria-busy="true"]')).not.toBeNull()
  })

  it('announces an unavailable snapshot and retries on demand', async () => {
    const user = userEvent.setup()
    const { props } = renderPage({
      data: { available: false, project_id: 'demo', reason: 'store offline' },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('Mission Control is unavailable')
    expect(screen.getByText('store offline')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(props.onRefresh).toHaveBeenCalledTimes(1)
  })

  it('requests a plan for the exact actionable alert', async () => {
    const user = userEvent.setup()
    const { props } = renderPage()
    await user.click(screen.getByRole('button', { name: 'Review governed action' }))
    expect(props.onPlanAction).toHaveBeenCalledTimes(1)
    expect(props.onPlanAction).toHaveBeenCalledWith(actionableAlert)
  })

  it('shows the immutable digest and confirms with digest plus operator identity', async () => {
    const user = userEvent.setup()
    const { props } = renderPage({ actionData: plannedAction })

    expect(screen.getByText(PLAN_DIGEST)).toBeInTheDocument()
    expect(
      screen.getByText('Recovers run-1 and requeues its outbox work.'),
    ).toBeInTheDocument()

    const input = screen.getByLabelText('Operator ID')
    await user.clear(input)
    await user.type(input, 'ops-kim')
    await user.click(screen.getByRole('button', { name: 'Confirm exact plan' }))

    expect(props.onExecuteAction).toHaveBeenCalledTimes(1)
    expect(props.onExecuteAction).toHaveBeenCalledWith('action-1', PLAN_DIGEST, 'ops-kim')
  })

  it('refuses to confirm without an operator identity', async () => {
    const user = userEvent.setup()
    const { props } = renderPage({ actionData: plannedAction })

    const input = screen.getByLabelText('Operator ID')
    await user.clear(input)
    expect(screen.getByRole('button', { name: 'Confirm exact plan' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Confirm exact plan' }))
    expect(props.onExecuteAction).not.toHaveBeenCalled()
  })

  it('replaces the confirm control with a receipt once executed', () => {
    renderPage({
      actionData: {
        ...plannedAction,
        phase: 'execute',
        action: {
          ...plannedAction.action!,
          status: 'executed',
          operator_id: 'ops-kim',
          executed_at: '2026-07-28T12:05:00Z',
        },
      },
    })
    expect(screen.getByText(/Execution receipt recorded/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm exact plan' })).toBeNull()
  })

  it('surfaces rejected plans as errors', () => {
    renderPage({
      actionData: {
        ok: false,
        phase: 'plan',
        project_id: 'demo',
        error: 'action kind is not allowlisted',
      },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('Action was not accepted')
    expect(screen.getByText('action kind is not allowlisted')).toBeInTheDocument()
  })

  it('renders the empty state when no alerts are open', () => {
    renderPage({ data: { available: true, project_id: 'demo', alerts: [] } })
    expect(screen.getByText('No active alerts')).toBeInTheDocument()
  })

  it('renders provider capacity with readiness verdicts and recommendations', () => {
    renderPage({
      data: {
        available: true,
        project_id: 'demo',
        alerts: [],
        provider_slo: {
          claude: {
            samples: 48,
            availability: 1,
            p95_latency_ms: 420,
            target_met: true,
            production_ready: true,
          },
          codex: {
            samples: 3,
            availability: 0.5,
            p95_latency_ms: 900,
            target_met: false,
            production_ready: false,
            blockers: ['observation window below 24h'],
          },
        },
        provider_call_quotas: {
          claude: {
            enabled: true,
            allowed: true,
            used: 40,
            remaining: 160,
            limit: 200,
            window_seconds: 86_400,
          },
        },
        recommendations: ['Continue the codex readiness campaign.'],
      },
    })

    expect(screen.getByRole('heading', { name: 'Provider capacity' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'claude' })).toBeInTheDocument()
    expect(screen.getByText('Production ready')).toBeInTheDocument()
    expect(screen.getByText('Evidence pending')).toBeInTheDocument()
    expect(screen.getByText('observation window below 24h')).toBeInTheDocument()
    expect(screen.getByText('40 / 200')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Recommended next' })).toBeInTheDocument()
    expect(screen.getByText('Continue the codex readiness campaign.')).toBeInTheDocument()
  })

  it('renders benchmark evidence stages without implying trusted promotion', () => {
    renderPage({
      data: {
        available: true,
        project_id: 'demo',
        alerts: [],
        evidence_funnel: {
          all_runs: {
            started: 10,
            completed: 9,
            scored: 8,
            accepted: 7,
            awaiting_judgment: 1,
          },
          benchmark: {
            started: 6,
            completed: 5,
            scored: 3,
            accepted: 2,
            awaiting_judgment: 2,
          },
        },
      },
    })

    expect(screen.getByRole('heading', { name: 'Evidence funnel' })).toBeInTheDocument()
    expect(screen.getByText('Benchmark runs')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Started evidence' })).toHaveAttribute('aria-valuenow', '6')
    expect(screen.getByRole('progressbar', { name: 'Accepted evidence' })).toHaveAttribute('aria-valuenow', '2')
    expect(screen.getByText(/does not imply trusted-pair validation/)).toBeInTheDocument()
    expect(screen.getByText(/2 completed run\(s\) still await judgment/)).toBeInTheDocument()
  })

  it('lists runs awaiting judgment with run id and benchmark slot', () => {
    renderPage({
      data: {
        available: true,
        project_id: 'demo',
        alerts: [],
        judgment_queue: [
          {
            run_id: 'run-judge-1',
            goal_id: 'goal-1',
            completed_at: '2026-07-28T10:00:00Z',
            benchmark_slot_id: 'slot-7',
          },
          { run_id: 'run-judge-2', goal_id: 'goal-1' },
        ],
      },
    })

    expect(screen.getByRole('heading', { name: 'Judgment queue' })).toBeInTheDocument()
    expect(screen.getByText('run-judge-1')).toBeInTheDocument()
    expect(screen.getByText('slot-7')).toBeInTheDocument()
    expect(screen.getByText('run-judge-2')).toBeInTheDocument()
    expect(screen.queryByText('No runs awaiting judgment')).toBeNull()
  })

  it('shows the judgment queue empty state when every completed run is scored', () => {
    renderPage({
      data: { available: true, project_id: 'demo', alerts: [], judgment_queue: [] },
    })
    expect(screen.getByText('No runs awaiting judgment')).toBeInTheDocument()
  })
})

describe('MissionControlPage judgment queue draft commands', () => {
  it('renders a copyable judge draft command when slot and campaign are known', () => {
    renderPage({
      data: {
        available: true,
        project_id: 'demo',
        alerts: [],
        judgment_queue: [
          {
            run_id: 'benchmark-pilot-software-race-fix-r1-company',
            goal_id: 'g1',
            benchmark_slot_id: 'software-race-fix/company/1',
            benchmark_campaign_id: 'pilot-2026q3',
          },
        ],
      },
    })
    const command = screen.getByText(
      /opc ops judge draft benchmark-pilot-software-race-fix-r1-company/,
    )
    expect(command).toHaveTextContent(
      'outputs/benchmark/artifacts/pilot-2026q3/software-race-fix-r1/company',
    )
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('omits the command when the run is not a benchmark slot', () => {
    renderPage({
      data: {
        available: true,
        project_id: 'demo',
        alerts: [],
        judgment_queue: [{ run_id: 'adhoc-run', goal_id: 'g2' }],
      },
    })
    expect(screen.getByText('adhoc-run')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull()
  })
})
