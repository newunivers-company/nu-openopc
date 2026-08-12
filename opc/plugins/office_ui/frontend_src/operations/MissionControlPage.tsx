import { useState } from 'react'
import type {
  MissionActionPayload,
  MissionControlAlert,
  MissionControlEvidenceStage,
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

function formatBytes(value: number | undefined): string {
  const bytes = Math.max(0, numberOrZero(value))
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let amount = bytes
  let unit = -1
  do {
    amount /= 1024
    unit += 1
  } while (amount >= 1024 && unit < units.length - 1)
  return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${units[unit]}`
}

function commandText(parts?: string[]): string {
  return (parts ?? []).map(part => (
    /^[A-Za-z0-9_./:-]+$/.test(part) ? part : JSON.stringify(part)
  )).join(' ')
}

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

function judgeDraftCommand(entry: {
  run_id: string
  benchmark_slot_id?: string
  benchmark_campaign_id?: string
}): string {
  const slot = entry.benchmark_slot_id ?? ''
  const campaign = entry.benchmark_campaign_id ?? ''
  if (!slot || !campaign) return ''
  const [caseId, mode, repetition] = slot.split('/')
  if (!caseId || !mode || !repetition) return ''
  const artifactDir = `outputs/benchmark/artifacts/${campaign}/${caseId}-r${repetition}/${mode}`
  return `opc ops judge draft ${entry.run_id} --artifacts ${artifactDir} --output ${entry.run_id}.draft.json`
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

function EvidenceFunnel({ data }: {
  data?: MissionControlPayload['evidence_funnel']
}) {
  const benchmark = data?.benchmark
  const allRuns = data?.all_runs
  const benchmarkStarted = numberOrZero(benchmark?.started)
  const scope = benchmarkStarted > 0 ? benchmark : allRuns
  const scopeLabel = benchmarkStarted > 0 ? 'Benchmark runs' : 'All governed runs'
  const started = numberOrZero(scope?.started)
  const stages: Array<{ key: keyof MissionControlEvidenceStage; label: string }> = [
    { key: 'started', label: 'Started' },
    { key: 'completed', label: 'Terminal' },
    { key: 'scored', label: 'Judged' },
    { key: 'accepted', label: 'Accepted' },
  ]
  return (
    <section className="mc-evidence" aria-labelledby="mc-evidence-title">
      <div className="mc-evidence-heading">
        <div>
          <span className="mc-section-index">00</span>
          <h2 id="mc-evidence-title">Evidence funnel</h2>
        </div>
        <span>{scopeLabel}</span>
      </div>
      <div className="mc-funnel">
        {stages.map(({ key, label }, index) => {
          const value = numberOrZero(scope?.[key])
          const width = started > 0 ? Math.max(8, Math.round((value / started) * 100)) : 0
          return (
            <article className="mc-funnel-stage" key={key}>
              <div>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
              <div
                className="mc-funnel-track"
                role="progressbar"
                aria-label={`${label} evidence`}
                aria-valuemin={0}
                aria-valuemax={Math.max(started, 1)}
                aria-valuenow={value}
              >
                <span style={{ width: `${width}%` }} />
              </div>
              {index < stages.length - 1 && <span className="mc-funnel-arrow" aria-hidden="true">→</span>}
            </article>
          )
        })}
      </div>
      <p>
        Accepted means a persisted passing scorecard. It does not imply trusted-pair
        validation or learning-asset promotion.
        {numberOrZero(scope?.awaiting_judgment) > 0 && (
          <> {numberOrZero(scope?.awaiting_judgment)} completed run(s) still await judgment.</>
        )}
      </p>
    </section>
  )
}

function CampaignCockpit({ data }: {
  data?: MissionControlPayload['campaign_portfolio']
}) {
  const campaign = data?.active_campaign
  if (!campaign) return null
  const gate = campaign.batch_expansion
  const workloads = Object.entries(campaign.workloads ?? {})
  return (
    <section className="mc-campaign" aria-labelledby="mc-campaign-title">
      <div className="mc-campaign-heading">
        <div>
          <span className="mc-section-index">00B</span>
          <h2 id="mc-campaign-title">Campaign cockpit</h2>
        </div>
        <code>{campaign.campaign_id}</code>
      </div>
      <div className="mc-campaign-gate">
        <div>
          <span>Expansion phase</span>
          <strong>{gate.phase.replaceAll('_', ' ')}</strong>
        </div>
        <div>
          <span>Next pair budget</span>
          <strong>{gate.next_pair_budget}</strong>
        </div>
        <div>
          <span>Trusted pairs</span>
          <strong>{campaign.trusted_pairs}</strong>
        </div>
        <div>
          <span>Awaiting judgment</span>
          <strong>{campaign.awaiting_judgment}</strong>
        </div>
      </div>
      {workloads.length > 0 && (
        <ul className="mc-campaign-workloads">
          {workloads.map(([workload, stats]) => (
            <li key={workload}>
              <span>{workload}</span>
              <strong>{stats.trusted_pairs} trusted pair(s)</strong>
              <span>{stats.judged} / {stats.started} judged</span>
            </li>
          ))}
        </ul>
      )}
      {(gate.blockers ?? []).length > 0 && (
        <div className="mc-campaign-blockers">
          Blocked by: {(gate.blockers ?? []).map(item => item.replaceAll('_', ' ')).join(', ')}
        </div>
      )}
      <p><strong>Next authorized action:</strong> {gate.next_action}</p>
      <small>
        Repository-derived operating estimate only. Promotion still requires the
        sealed observation ledger and independent dossier gates.
      </small>
    </section>
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

function StorageBlock({ storage }: { storage?: MissionControlPayload['storage'] }) {
  if (!storage?.available) {
    return (
      <div className="mc-empty mc-empty--compact">
        <strong>Storage inventory unavailable</strong>
        <span>{storage?.reason ?? 'The runtime has not bound its storage root yet.'}</span>
      </div>
    )
  }
  const candidates = numberOrZero(storage.candidate_count)
  const inspectCommand = commandText(storage.inspect_command)
  return (
    <div className="mc-storage">
      <div className="mc-storage-total">
        <div>
          <span>Total footprint</span>
          <strong>{formatBytes(storage.total_bytes)}</strong>
        </div>
        <span>{numberOrZero(storage.file_count)} files · dry-run only</span>
      </div>
      <dl className="mc-storage-breakdown">
        <div><dt>Databases</dt><dd>{formatBytes(storage.database_bytes)}</dd></div>
        <div><dt>Backups</dt><dd>{formatBytes(storage.backup_bytes)}</dd></div>
        <div><dt>Logs</dt><dd>{formatBytes(storage.log_bytes)}</dd></div>
        <div><dt>Reclaimable</dt><dd>{formatBytes(storage.candidate_bytes)}</dd></div>
      </dl>
      <div className={`mc-storage-candidates${candidates > 0 ? ' is-pending' : ''}`}>
        <strong>{candidates} retention candidate{candidates === 1 ? '' : 's'}</strong>
        <span>
          Keep {storage.policy?.keep_latest ?? 3} newest · older than {storage.policy?.max_age_days ?? 30} days
        </span>
      </div>
      {(storage.largest_files ?? []).length > 0 && (
        <ul className="mc-storage-largest" aria-label="Largest storage files">
          {(storage.largest_files ?? []).slice(0, 3).map(file => (
            <li key={file.path}><code>{file.path}</code><span>{formatBytes(file.size_bytes)}</span></li>
          ))}
        </ul>
      )}
      {inspectCommand && (
        <div className="mc-storage-command">
          <span>Inspect again</span>
          <code>{inspectCommand}</code>
        </div>
      )}
      <small>Automatic cleanup is disabled. Applying retention always requires a separate explicit <code>--apply</code>.</small>
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
  const judgmentQueue = (data.judgment_queue ?? []).slice(0, 20)

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

      <EvidenceFunnel data={data.evidence_funnel} />
      <CampaignCockpit data={data.campaign_portfolio} />

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

          <section className="mc-section" aria-labelledby="mc-storage-title">
            <div className="mc-section-heading">
              <div>
                <span className="mc-section-index">03</span>
                <h2 id="mc-storage-title">Storage inventory</h2>
              </div>
            </div>
            <StorageBlock storage={data.storage} />
          </section>

          <section className="mc-section" aria-labelledby="mc-next-title">
            <div className="mc-section-heading">
              <div>
                <span className="mc-section-index">04</span>
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

          <section className="mc-section" aria-labelledby="mc-judgment-title">
            <div className="mc-section-heading">
              <div>
                <span className="mc-section-index">05</span>
                <h2 id="mc-judgment-title">Judgment queue</h2>
              </div>
              <span>{judgmentQueue.length} waiting</span>
            </div>
            {judgmentQueue.length > 0 ? (
              <ul className="mc-judgment-list">
                {judgmentQueue.map(entry => {
                  const draftCommand = judgeDraftCommand(entry)
                  return (
                    <li key={entry.run_id}>
                      <div className="mc-judgment-main">
                        <code>{entry.run_id}</code>
                        {entry.benchmark_slot_id && (
                          <span className="mc-judgment-slot">{entry.benchmark_slot_id}</span>
                        )}
                      </div>
                      {entry.completed_at && (
                        <span className="mc-judgment-time">{formatTimestamp(entry.completed_at)}</span>
                      )}
                      {draftCommand && (
                        <div className="mc-judgment-command">
                          <code>{draftCommand}</code>
                          <button
                            type="button"
                            className="mc-action-btn"
                            onClick={() => {
                              void navigator.clipboard?.writeText(draftCommand)
                            }}
                          >
                            Copy
                          </button>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            ) : (
              <div className="mc-empty mc-empty--compact">
                <strong>No runs awaiting judgment</strong>
                <span>Every completed run has a persisted scorecard.</span>
              </div>
            )}
          </section>
        </aside>
      </div>
    </main>
  )
}
