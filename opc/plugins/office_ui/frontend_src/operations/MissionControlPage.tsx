import { useState } from 'react'
import type {
  MissionActionPayload,
  MissionControlAlert,
  MissionControlPayload,
  MissionControlProviderQuota,
  MissionControlProviderSlo,
} from '../lib/wsClient'

interface MissionControlPageProps {
  data: MissionControlPayload | null
  loading: boolean
  onRefresh: () => void
  actionData: MissionActionPayload | null
  actionLoading: boolean
  onPlanAction: (alert: MissionControlAlert) => void
  onExecuteAction: (
    actionId: string,
    planDigest: string,
    operatorId: string,
  ) => void
}

const numberOrZero = (value: number | undefined): number => (
  Number.isFinite(value) ? Number(value) : 0
)

const percent = (value: number | undefined): string => (
  `${Math.round(numberOrZero(value) * 100)}%`
)

const severityRank: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
}

function formatTimestamp(value?: string): string {
  if (!value) return 'Awaiting first snapshot'
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(timestamp)
}

function healthSummary(alerts: MissionControlAlert[]): { label: string; tone: string } {
  if (alerts.some(alert => alert.severity === 'critical')) return { label: 'Action required', tone: 'critical' }
  if (alerts.some(alert => alert.severity === 'high')) return { label: 'Attention needed', tone: 'warning' }
  if (alerts.length > 0) return { label: 'Monitor', tone: 'monitor' }
  return { label: 'Operating normally', tone: 'healthy' }
}

function MetricCard({ label, value, detail, tone = 'neutral' }: {
  label: string
  value: string | number
  detail: string
  tone?: 'neutral' | 'warning' | 'critical'
}) {
  return (
    <article className={`mc-metric mc-metric--${tone}`}>
      <span className="mc-metric-label">{label}</span>
      <strong className="mc-metric-value">{value}</strong>
      <span className="mc-metric-detail">{detail}</span>
    </article>
  )
}

function AlertRow({ alert, onPlanAction, actionLoading }: {
  alert: MissionControlAlert
  onPlanAction: (alert: MissionControlAlert) => void
  actionLoading: boolean
}) {
  const severity = alert.severity.toLowerCase()
  const actionable = Boolean(alert.action_kind && alert.action_target_id)
  return (
    <li className={`mc-alert mc-alert--${severity}`}>
      <div className="mc-alert-rail" aria-hidden="true" />
      <div className="mc-alert-copy">
        <div className="mc-alert-meta">
          <span className="mc-alert-severity">{severity}</span>
          <span>{alert.kind.replaceAll('_', ' ')}</span>
        </div>
        <strong>{alert.title}</strong>
        <p>{alert.detail}</p>
        {alert.action && <div className="mc-alert-action">Next: {alert.action}</div>}
        {actionable && (
          <button
            type="button"
            className="mc-action-btn"
            onClick={() => onPlanAction(alert)}
            disabled={actionLoading}
          >
            Review governed action
          </button>
        )}
      </div>
    </li>
  )
}

function ActionReview({ data, loading, operatorId, onOperatorId, onExecute }: {
  data: MissionActionPayload | null
  loading: boolean
  operatorId: string
  onOperatorId: (value: string) => void
  onExecute: (actionId: string, planDigest: string, operatorId: string) => void
}) {
  if (!data && !loading) return null
  if (loading && !data) {
    return (
      <div className="mc-action-review" role="status">
        Preparing an immutable action plan…
      </div>
    )
  }
  if (!data?.ok || !data.action) {
    return (
      <div className="mc-action-review mc-action-review--error" role="alert">
        <strong>Action was not accepted</strong>
        <span>{data?.error ?? 'No action receipt was returned.'}</span>
      </div>
    )
  }
  const action = data.action
  const planned = action.status === 'planned'
  return (
    <div className={`mc-action-review mc-action-review--${action.status}`} aria-live="polite">
      <div className="mc-action-review-head">
        <div>
          <span>GOVERNED ACTION / {action.status.toUpperCase()}</span>
          <strong>{action.kind.replaceAll('_', ' ')}</strong>
        </div>
        <code>{action.target_id}</code>
      </div>
      <p>{action.consequence}</p>
      <dl>
        <div><dt>Plan digest</dt><dd><code>{action.plan_digest}</code></dd></div>
        <div><dt>Expires</dt><dd>{formatTimestamp(action.expires_at ?? undefined)}</dd></div>
        {action.operator_id && <div><dt>Operator</dt><dd>{action.operator_id}</dd></div>}
      </dl>
      {planned ? (
        <div className="mc-action-confirm">
          <label>
            Operator ID
            <input
              value={operatorId}
              onChange={event => onOperatorId(event.target.value)}
              autoComplete="username"
            />
          </label>
          <button
            type="button"
            className="mc-action-btn mc-action-btn--confirm"
            disabled={loading || operatorId.trim().length === 0}
            onClick={() => onExecute(
              action.action_id,
              action.plan_digest,
              operatorId.trim(),
            )}
          >
            {loading ? 'Executing…' : 'Confirm exact plan'}
          </button>
        </div>
      ) : (
        <div className="mc-action-receipt">
          {action.status === 'executed'
            ? `Execution receipt recorded ${formatTimestamp(action.executed_at ?? undefined)}.`
            : `Action ended with status: ${action.status}.`}
        </div>
      )}
    </div>
  )
}

function SloBlock({ slo }: { slo?: MissionControlProviderSlo }) {
  if (!slo) return <span className="mc-provider-empty">No canary samples</span>
  const productionReady = slo.production_ready === true
  const readinessLabel = productionReady ? 'Production ready' : 'Evidence pending'
  return (
    <div className="mc-provider-measure">
      <div className="mc-provider-measure-head">
        <span>Availability</span>
        <strong className={slo.target_met ? 'is-good' : 'is-risk'}>{percent(slo.availability)}</strong>
      </div>
      <div
        className="mc-progress"
        role="progressbar"
        aria-label="Provider availability"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(numberOrZero(slo.availability) * 100)}
      >
        <span style={{ width: `${Math.min(100, Math.max(0, numberOrZero(slo.availability) * 100))}%` }} />
      </div>
      <span>{slo.samples} samples · p95 {Math.round(numberOrZero(slo.p95_latency_ms))} ms</span>
      <div className="mc-provider-readiness">
        <strong className={productionReady ? 'is-good' : 'is-risk'}>{readinessLabel}</strong>
        {!productionReady && (slo.blockers ?? []).length > 0 && (
          <span>{slo.blockers?.[0]}</span>
        )}
      </div>
    </div>
  )
}

function QuotaBlock({ quota }: { quota?: MissionControlProviderQuota }) {
  if (!quota?.enabled || quota.limit <= 0) return <span className="mc-provider-empty">Call quota not configured</span>
  const utilization = Math.min(1, quota.used / quota.limit)
  return (
    <div className="mc-provider-measure">
      <div className="mc-provider-measure-head">
        <span>Rolling call quota</span>
        <strong className={quota.allowed ? '' : 'is-risk'}>{quota.used} / {quota.limit}</strong>
      </div>
      <div
        className={`mc-progress${quota.allowed ? '' : ' is-exhausted'}`}
        role="progressbar"
        aria-label="Provider call quota utilization"
        aria-valuemin={0}
        aria-valuemax={quota.limit}
        aria-valuenow={quota.used}
      >
        <span style={{ width: `${Math.round(utilization * 100)}%` }} />
      </div>
      <span>{quota.remaining} remaining · {Math.round(quota.window_seconds / 3600)}h window</span>
    </div>
  )
}

export function MissionControlPage({
  data,
  loading,
  onRefresh,
  actionData,
  actionLoading,
  onPlanAction,
  onExecuteAction,
}: MissionControlPageProps) {
  const [operatorId, setOperatorId] = useState(() => {
    try {
      return localStorage.getItem('opc_operator_id') || 'owner'
    } catch {
      return 'owner'
    }
  })
  const updateOperatorId = (value: string) => {
    setOperatorId(value)
    try {
      localStorage.setItem('opc_operator_id', value)
    } catch {
      // Private browsing may reject storage; the current input still works.
    }
  }
  if (!data && loading) {
    return (
      <section className="mission-control-page mc-state" aria-busy="true" aria-live="polite">
        <div className="mc-state-indicator" aria-hidden="true" />
        <strong>Building the operations snapshot</strong>
        <span>Reading durable runs, gates, approvals, provider health, and quotas.</span>
      </section>
    )
  }

  if (!data || !data.available) {
    return (
      <section className="mission-control-page mc-state" role="alert">
        <span className="mc-state-code">OPS / UNAVAILABLE</span>
        <strong>Mission Control is unavailable</strong>
        <span>{data?.reason ?? 'No operations snapshot has been received.'}</span>
        <button type="button" className="mc-refresh-btn" onClick={onRefresh} disabled={loading}>
          Retry
        </button>
      </section>
    )
  }

  const alerts = [...(data.alerts ?? [])].sort((left, right) => (
    (severityRank[left.severity] ?? 99) - (severityRank[right.severity] ?? 99)
  ))
  const health = healthSummary(alerts)
  const providerSlo = data.provider_slo ?? {}
  const providerQuotas = data.provider_call_quotas ?? {}
  const providerNames = Array.from(new Set([
    ...Object.keys(providerSlo),
    ...Object.keys(providerQuotas),
  ])).sort()

  return (
    <main className="mission-control-page" aria-busy={loading}>
      <header className="mc-header">
        <div>
          <div className="mc-overline">OPERATIONS / {data.project_id.toUpperCase()}</div>
          <h1>Mission Control</h1>
          <p>Evidence-backed status for durable execution, quality gates, and provider capacity.</p>
        </div>
        <div className="mc-header-actions">
          <span className={`mc-health mc-health--${health.tone}`}>
            <span aria-hidden="true" />{health.label}
          </span>
          <span className="mc-generated">Updated {formatTimestamp(data.generated_at)}</span>
          <button type="button" className="mc-refresh-btn" onClick={onRefresh} disabled={loading}>
            {loading ? 'Refreshing…' : 'Refresh snapshot'}
          </button>
        </div>
      </header>

      <section className="mc-metrics" aria-label="Operations summary">
        <MetricCard
          label="Active work"
          value={numberOrZero(data.active_runs)}
          detail={`${numberOrZero(data.active_goals)} goals · ${numberOrZero(data.blocked_runs)} blocked`}
          tone={numberOrZero(data.blocked_runs) > 0 ? 'warning' : 'neutral'}
        />
        <MetricCard
          label="Quality gates"
          value={numberOrZero(data.failed_gates)}
          detail={`${numberOrZero(data.average_score).toFixed(2)} average score`}
          tone={numberOrZero(data.failed_gates) > 0 ? 'critical' : 'neutral'}
        />
        <MetricCard
          label="Delivery queue"
          value={numberOrZero(data.pending_outbox)}
          detail={`${numberOrZero(data.dead_letters)} dead-letter · ${numberOrZero(data.pending_approvals)} approvals`}
          tone={numberOrZero(data.dead_letters) > 0 ? 'critical' : numberOrZero(data.pending_approvals) > 0 ? 'warning' : 'neutral'}
        />
        <MetricCard
          label="Provider evidence"
          value={providerNames.length}
          detail={`${numberOrZero(data.unmeasured_usage_events)} unmeasured · $${numberOrZero(data.total_cost_usd).toFixed(4)} tracked`}
          tone={numberOrZero(data.unmeasured_usage_events) > 0 ? 'warning' : 'neutral'}
        />
      </section>

      <div className="mc-content-grid">
        <section className="mc-section" aria-labelledby="mc-alerts-title">
          <div className="mc-section-heading">
            <div>
              <span className="mc-section-index">01</span>
              <h2 id="mc-alerts-title">Operational alerts</h2>
            </div>
            <span>{alerts.length} open</span>
          </div>
          <ActionReview
            data={actionData}
            loading={actionLoading}
            operatorId={operatorId}
            onOperatorId={updateOperatorId}
            onExecute={onExecuteAction}
          />
          {alerts.length > 0 ? (
            <ul className="mc-alert-list">
              {alerts.map((alert, index) => (
                <AlertRow
                  key={`${alert.kind}-${alert.action_target_id ?? alert.run_id ?? ''}-${index}`}
                  alert={alert}
                  onPlanAction={onPlanAction}
                  actionLoading={actionLoading}
                />
              ))}
            </ul>
          ) : (
            <div className="mc-empty">
              <strong>No active alerts</strong>
              <span>Durable runs, gates, approvals, and queues are within current policy.</span>
            </div>
          )}
        </section>

        <aside className="mc-side-stack">
          <section className="mc-section" aria-labelledby="mc-providers-title">
            <div className="mc-section-heading">
              <div>
                <span className="mc-section-index">02</span>
                <h2 id="mc-providers-title">Provider capacity</h2>
              </div>
            </div>
            {providerNames.length > 0 ? (
              <div className="mc-provider-list">
                {providerNames.map(provider => (
                  <article className="mc-provider" key={provider}>
                    <h3>{provider}</h3>
                    <SloBlock slo={providerSlo[provider]} />
                    <QuotaBlock quota={providerQuotas[provider]} />
                  </article>
                ))}
              </div>
            ) : (
              <div className="mc-empty mc-empty--compact">
                <strong>No provider telemetry yet</strong>
                <span>Canary and quota evidence appears after the first governed provider call.</span>
              </div>
            )}
          </section>

          <section className="mc-section" aria-labelledby="mc-next-title">
            <div className="mc-section-heading">
              <div>
                <span className="mc-section-index">03</span>
                <h2 id="mc-next-title">Recommended next</h2>
              </div>
            </div>
            {(data.recommendations ?? []).length > 0 ? (
              <ol className="mc-recommendations">
                {(data.recommendations ?? []).map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
              </ol>
            ) : (
              <div className="mc-empty mc-empty--compact">
                <strong>No intervention recommended</strong>
                <span>Continue monitoring current execution and provider health.</span>
              </div>
            )}
          </section>
        </aside>
      </div>
    </main>
  )
}
