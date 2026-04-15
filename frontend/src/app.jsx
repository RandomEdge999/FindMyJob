
import { useEffect, useMemo, useState } from 'react'
import { Link, Route, Routes, useLocation, useSearchParams } from 'react-router-dom'

const NAV_ITEMS = [
  { label: 'Dashboard', to: '/' },
  { label: 'Setup', to: '/setup' },
  { label: 'Settings', to: '/settings' },
  { label: 'Autopilot', to: '/autopilot' },
  { label: 'Review', to: '/review' },
  { label: 'Runs', to: '/runs' },
]

const STAGE_LABELS = {
  idle: 'Idle',
  queue: 'Queue',
  discovery: 'Discovery',
  screening: 'Screening',
  evaluation: 'Evaluation',
  drafting: 'Drafting',
  prepare: 'Prepare',
  review: 'Review',
  preview: 'Preview',
  submit: 'Submit',
  question_resolution: 'Questions',
  complete: 'Complete',
}

const STAGE_ORDER = ['queue', 'discovery', 'screening', 'evaluation', 'drafting', 'prepare', 'review', 'preview', 'submit', 'question_resolution', 'complete']
const TERMINAL_STATUSES = new Set(['completed', 'completed_with_failures', 'failed', 'interrupted', 'cancelled'])

async function requestJson(url, init) {
  const timeoutMs = Number(init?.timeoutMs)
  const effectiveTimeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 30_000
  const timeoutSeconds = Math.round(effectiveTimeoutMs / 1000)
  const controller = new AbortController()
  const upstreamSignal = init?.signal
  let timedOut = false
  const onAbort = () => controller.abort(upstreamSignal?.reason)
  if (upstreamSignal) {
    if (upstreamSignal.aborted) {
      onAbort()
    } else {
      upstreamSignal.addEventListener('abort', onAbort, { once: true })
    }
  }
  const timeoutId = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, effectiveTimeoutMs)
  let response
  const { timeoutMs: _timeoutMs, ...requestInit } = init || {}
  try {
    response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(requestInit.headers || {}) },
      ...requestInit,
      signal: controller.signal,
    })
  } catch (err) {
    if (timedOut || (err instanceof Error && err.name === 'AbortError' && !upstreamSignal?.aborted)) {
      throw new Error(`Request timed out after ${timeoutSeconds} seconds`)
    }
    throw err
  } finally {
    window.clearTimeout(timeoutId)
    upstreamSignal?.removeEventListener('abort', onAbort)
  }
  if (!response.ok) {
    const payload = await response.text()
    let message = payload
    if (payload) {
      try {
        const parsed = JSON.parse(payload)
        message = parsed?.detail || parsed?.message || payload
      } catch {
        message = payload
      }
    }
    throw new Error(message || `Request failed: ${response.status}`)
  }
  if (response.status === 204) {
    return null
  }
  return response.json()
}

function usePageVisible() {
  const [visible, setVisible] = useState(typeof document === 'undefined' ? true : !document.hidden)

  useEffect(() => {
    const update = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', update)
    window.addEventListener('focus', update)
    window.addEventListener('blur', update)
    return () => {
      document.removeEventListener('visibilitychange', update)
      window.removeEventListener('focus', update)
      window.removeEventListener('blur', update)
    }
  }, [])

  return visible
}

function usePolledJson(url, intervalMs = 5000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const visible = usePageVisible()

  useEffect(() => {
    let active = true
    let timer = null

    const load = async () => {
      try {
        const payload = await requestJson(url)
        if (!active) return
        setData(payload)
        setError('')
      } catch (err) {
        if (!active) return
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (active) {
          setLoading(false)
          timer = window.setTimeout(load, visible ? intervalMs : Math.max(intervalMs * 3, 15000))
        }
      }
    }

    load()
    return () => {
      active = false
      if (timer) window.clearTimeout(timer)
    }
  }, [intervalMs, url, visible])

  const refresh = async () => {
    const payload = await requestJson(url)
    setData(payload)
    setError('')
    setLoading(false)
    return payload
  }

  return { data, error, loading, refresh }
}

function normalizeChoice(value) {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, ' ')
}

function dedupeStrings(values) {
  const seen = []
  values.forEach((value) => {
    const cleaned = String(value ?? '').trim()
    if (cleaned && !seen.includes(cleaned)) seen.push(cleaned)
  })
  return seen
}

function questionOptions(question) {
  const optionDetails = Array.isArray(question?.option_details) && question.option_details.length
    ? question.option_details
    : Array.isArray(question?.option_signature)
      ? question.option_signature.map((option) => ({ label: option, value: option }))
      : []
  return optionDetails
    .map((option) => {
      const label = String(option?.label ?? option?.value ?? option?.id ?? '').trim()
      const value = String(option?.value ?? option?.id ?? option?.label ?? '').trim()
      if (!label && !value) return null
      return { label: label || value, value: value || label }
    })
    .filter(Boolean)
}

function matchQuestionOption(options, rawValue) {
  const normalized = normalizeChoice(rawValue)
  if (!normalized) return null
  return options.find((option) => normalized === normalizeChoice(option.label) || normalized === normalizeChoice(option.value)) || null
}

function parseMultiAnswer(rawValue) {
  if (Array.isArray(rawValue)) return dedupeStrings(rawValue)
  return dedupeStrings(String(rawValue ?? '').split(/[\n,;|]/))
}

function hydrateAnswerDraft(question, rawAnswer) {
  const options = questionOptions(question)
  if (question?.widget_type === 'checkbox_group') {
    return parseMultiAnswer(rawAnswer).map((value) => matchQuestionOption(options, value)?.label || value)
  }
  if (!options.length) return String(rawAnswer ?? '')
  return matchQuestionOption(options, rawAnswer)?.label || String(rawAnswer ?? '')
}

function serializeAnswerDraft(question, draftValue) {
  if (question?.widget_type === 'checkbox_group') {
    return parseMultiAnswer(draftValue).join(', ')
  }
  return String(draftValue ?? '').trim()
}

function useLiveConsole() {
  const visible = usePageVisible()
  const [snapshot, setSnapshot] = useState({ state: null, events: [] })
  const [error, setError] = useState('')
  const [connection, setConnection] = useState('connecting')
  const [lastSnapshotAt, setLastSnapshotAt] = useState(Date.now())

  const refresh = async () => {
    const payload = await requestJson('/api/live/status?limit=60')
    setSnapshot(payload)
    setError('')
    setConnection('connected')
    setLastSnapshotAt(Date.now())
    return payload
  }

  useEffect(() => {
    let active = true
    let fallbackTimer = null

    const pull = async () => {
      try {
        const payload = await requestJson('/api/live/status?limit=60')
        if (!active) return
        setSnapshot(payload)
        setError('')
        setConnection('connected')
        setLastSnapshotAt(Date.now())
      } catch (err) {
        if (!active) return
        setError(err instanceof Error ? err.message : String(err))
        setConnection('reconnecting')
      }
    }

    pull()

    const stream = new EventSource('/api/live/events?limit=60')
    const handlePayload = (event) => {
      try {
        const payload = JSON.parse(event.data)
        if (!active) return
        setSnapshot(payload)
        setError('')
        setConnection('connected')
        setLastSnapshotAt(Date.now())
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : String(err))
      }
    }
    const handleHeartbeat = () => {
      if (!active) return
      setConnection('connected')
      setLastSnapshotAt(Date.now())
    }
    const handleOpen = () => {
      if (active) setConnection('connected')
    }
    const handleError = () => {
      if (!active) return
      setConnection('reconnecting')
      if (!fallbackTimer) {
        fallbackTimer = window.setInterval(pull, visible ? 5000 : 15000)
      }
    }

    stream.addEventListener('snapshot', handlePayload)
    stream.addEventListener('update', handlePayload)
    stream.addEventListener('heartbeat', handleHeartbeat)
    stream.onopen = handleOpen
    stream.onerror = handleError

    return () => {
      active = false
      stream.removeEventListener('snapshot', handlePayload)
      stream.removeEventListener('update', handlePayload)
      stream.removeEventListener('heartbeat', handleHeartbeat)
      stream.onopen = null
      stream.onerror = null
      stream.close()
      if (fallbackTimer) window.clearInterval(fallbackTimer)
    }
  }, [visible])

  return { snapshot, error, connection, lastSnapshotAt, refresh }
}

function safeNumber(value, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function formatNumber(value) {
  return new Intl.NumberFormat('en-US').format(safeNumber(value, 0))
}

function parseTimestamp(value) {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

function formatDate(value) {
  const parsed = parseTimestamp(value)
  if (parsed === null) return '-'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed)
}

function formatRelativeAge(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '-'
  if (ms < 1000) return 'just now'
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`
  return `${Math.round(ms / 86_400_000)}d ago`
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '-'
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

function compactList(values, limit = 3) {
  const items = Array.isArray(values) ? values.filter(Boolean) : []
  if (!items.length) return ''
  if (items.length <= limit) return items.join(' / ')
  return `${items.slice(0, limit).join(' / ')} +${items.length - limit}`
}

function blockerLabel(blocker) {
  if (!blocker) return ''
  if (typeof blocker === 'string') return blocker
  if (typeof blocker === 'object') return blocker.label || blocker.category || JSON.stringify(blocker)
  return String(blocker)
}

function toneFor(value) {
  const text = String(value || '').toLowerCase()
  if (!text) return 'neutral'
  if (text.includes('fail') || text.includes('rejected') || text.includes('blocked') || text.includes('error') || text.includes('stale')) return 'danger'
  if (text.includes('warning') || text.includes('preview') || text.includes('needs') || text.includes('reconnecting') || text.includes('queued') || text.includes('running') || text.includes('awaiting') || text.includes('downloading') || text.includes('validating')) return 'warning'
  if (text.includes('ready') || text.includes('submitted') || text.includes('completed') || text.includes('applied') || text.includes('connected') || text.includes('success')) return 'success'
  return 'neutral'
}

function toneForStream(value) {
  const text = String(value || '').toLowerCase()
  if (text === 'connected') return 'success'
  if (text === 'reconnecting' || text === 'connecting') return 'warning'
  if (text === 'stale') return 'danger'
  return 'neutral'
}

function badgeText(value) {
  if (value === null || value === undefined || value === '') return 'idle'
  return String(value)
}
function summarizeEventPayload(payload) {
  if (!payload || typeof payload !== 'object') return []
  const chips = []
  const push = (label, value) => {
    if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) return
    chips.push({ label, value: Array.isArray(value) ? value.length : value })
  }
  push('New Jobs', payload.new_jobs)
  push('Scanned', payload.discovered)
  push('Approved', payload.classifier_approved ?? payload.approved_count ?? payload.eligible_count ?? payload.evaluated)
  push('Rejected', payload.classifier_rejected ?? payload.rejected_count ?? payload.screened_out)
  push('Drafted', payload.pdfs)
  push('Submitted', payload.submitted_application_ids)
  push('Failed', payload.failed_application_ids)
  push('Artifacts', payload.artifact_paths)
  if (payload.review_result?.scores) {
    const scores = payload.review_result.scores
    push('Review', `${scores.resume ?? 0}/${scores.cover_letter ?? 0}/${scores.form ?? 0}`)
  }
  return chips.slice(0, 5)
}

function mappingEntries(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value)
    .map(([key, item]) => [key, safeNumber(item)])
    .filter(([, item]) => item > 0)
    .sort((left, right) => right[1] - left[1])
}

function describeDraftBatch(batch, configuredTarget) {
  const memberCount = safeNumber(batch?.member_count)
  if (!memberCount) return 'no active draft batch'
  const targetSize = safeNumber(batch?.target_size || memberCount)
  const configured = safeNumber(configuredTarget || targetSize)
  const baseStatus = batch?.handoff_status || batch?.status || 'waiting_for_batch'
  if (configured > targetSize) {
    return `only ${formatNumber(targetSize)} approved job${targetSize === 1 ? '' : 's'} were available; configured target ${formatNumber(configured)}. ${baseStatus}`
  }
  return `target ${formatNumber(targetSize)} of configured ${formatNumber(configured)}. ${baseStatus}`
}

function formatTemporaryChatState(enabled, lastResult) {
  if (enabled) return 'enabled'
  switch (String(lastResult || '').trim()) {
    case 'already_enabled':
      return 'enabled'
    case 'toggle_unavailable':
      return 'toggle unavailable'
    case 'click_failed':
      return 'enable failed'
    case 'enabled':
      return 'enabled'
    default:
      return '-'
  }
}

export function deriveOperatorState(snapshot, connection, lastSnapshotAt) {
  const state = snapshot?.state || {}
  const drafting = snapshot?.drafting && typeof snapshot.drafting === 'object' ? snapshot.drafting : {}
  const draftBatch = drafting?.batch && typeof drafting.batch === 'object' ? drafting.batch : {}
  const events = Array.isArray(snapshot?.events) ? snapshot.events : []
  const latestEvent = events.length ? events[events.length - 1] : null
  const startedAt = state.run_started_at || state.started_at || latestEvent?.created_at || null
  const lastEventAt = state.last_event_at || latestEvent?.created_at || state.updated_at || null
  const startedMs = parseTimestamp(startedAt)
  const lastEventMs = parseTimestamp(lastEventAt)
  const stats = state.stats && typeof state.stats === 'object' ? state.stats : {}
  const modelActivity = state.model_activity && typeof state.model_activity === 'object' ? state.model_activity : {}
  const runType = String(state.run_type || 'idle')
  const status = String(state.status || 'idle')
  const stage = String(state.stage || 'idle')
  const isRunning = ['running', 'queued', 'starting'].includes(status)
  const lastActivityMs = Number.isFinite(lastEventMs) ? Math.max(lastEventMs, lastSnapshotAt) : lastSnapshotAt
  const stale = isRunning && Number.isFinite(lastActivityMs) ? Date.now() - lastActivityMs > 15_000 : false
  const streamHealth = connection === 'reconnecting' ? 'reconnecting' : stale ? 'stale' : String(state.stream_health || connection || 'idle')
  const currentCompany = state.current_company || state.company || latestEvent?.company || ''
  const currentRole = state.current_role || state.role || latestEvent?.role || ''
  const currentTitle = state.current_title || compactList([currentCompany, currentRole], 2) || 'No active target'
  const modelBadge = compactList([modelActivity.role || stats.model_activity?.role, modelActivity.profile || stats.model_activity?.profile], 2) || '-'
  const readyThreshold = safeNumber(
    snapshot?.ready_to_apply_threshold ??
    snapshot?.data?.ready_to_apply_threshold ??
    stats.ready_to_apply_threshold
  )
  const temporaryChatEnabled = Boolean(drafting?.temporary_chat_enabled)
  const temporaryChatStatus = formatTemporaryChatState(temporaryChatEnabled, drafting?.temporary_chat_last_result)
  const counters = {
    discovered: safeNumber(stats.discovered ?? stats.discovery_scanned ?? stats.discover ?? stats.scanned),
    screenedOut: safeNumber(stats.screened_out ?? stats.classifier_rejected ?? stats.screened_rejected),
    evaluated: safeNumber(stats.evaluated ?? stats.applications_created),
    drafted: safeNumber(stats.drafted),
    readyToApply: safeNumber(stats.ready_to_apply ?? stats.ready_for_submit),
    submitted: safeNumber(stats.submitted ?? state.submitted_count),
    blockedByQuestions: safeNumber(stats.blocked_by_questions ?? state.blocked_applications),
    failed: safeNumber(stats.failed ?? state.failed_count),
    discoveryBoardsCompleted: safeNumber(stats.discovery_boards_completed),
    discoveryBoardsTotal: safeNumber(stats.discovery_boards_total),
    discoverySeedPages: safeNumber(stats.discovery_seed_pages),
    deterministicRejects: safeNumber(stats.deterministic_rejects ?? stats.rejected ?? stats.rejected_count),
  }
  const queue = {
    depth: safeNumber(state.queue_depth),
    blocked: safeNumber(state.blocked_applications),
    pendingQuestions: safeNumber(state.pending_questions),
    submitted: safeNumber(state.submitted_count),
    failed: safeNumber(state.failed_count),
    rejected: safeNumber(state.rejected_count),
  }
  const latestErrorRaw = String(state.latest_error || '')
  const hideRecoveredWorkerError =
    status === 'completed' &&
    latestErrorRaw.toLowerCase() === 'stale live state recovered without an active worker.'
  const latestMessage = state.latest_operator_message || latestEvent?.message || state.latest_error || 'No active run.'
  const stageTrail = STAGE_ORDER.map((item) => ({
    key: item,
    label: STAGE_LABELS[item] || item,
    active: stage === item,
    done: STAGE_ORDER.indexOf(item) < STAGE_ORDER.indexOf(stage),
  }))

  return {
    state,
    stats,
    runType,
    status,
    stage,
    isRunning,
    isTerminal: TERMINAL_STATUSES.has(status),
    currentTitle,
    currentSource: state.source || latestEvent?.source || '',
    latestMessage,
    latestError: hideRecoveredWorkerError ? '' : latestErrorRaw,
    warningNotice:
      status === 'interrupted'
        ? 'Previous run was interrupted. Reset operational state before starting a clean end-to-end test.'
        : streamHealth === 'stale'
          ? 'Live discovery or submission events stopped updating. Stop the stale backend and reset if needed.'
          : '',
    elapsed: Number.isFinite(startedMs) ? formatDuration(Date.now() - startedMs) : '0s',
    lastSeen: Number.isFinite(lastActivityMs) ? formatRelativeAge(Date.now() - lastActivityMs) : formatRelativeAge(Date.now() - lastSnapshotAt),
    modelBadge,
    modelRole: modelActivity.role || stats.model_activity?.role || '',
    modelProfile: modelActivity.profile || stats.model_activity?.profile || '',
    streamHealth,
    readyThreshold,
    temporaryChatStatus,
    temporaryChatCheckedAt: drafting?.temporary_chat_checked_at || '',
    counters,
    queue,
    events,
    eventsDescending: [...events].reverse(),
    latestEvent,
    latestEventMeta: summarizeEventPayload(latestEvent?.payload),
    stageTrail,
    sourceMix: mappingEntries(stats.source_mix),
    sourceMetrics: Object.entries(stats.source_metrics || {}),
    sourceWarnings: Array.isArray(stats.source_warnings) ? stats.source_warnings : [],
    zeroResultSources: Array.isArray(stats.zero_result_sources) ? stats.zero_result_sources : [],
    discoveryErrors: mappingEntries(stats.discovery_error_counts),
    screenedOutReasons: mappingEntries(stats.screened_out_reasons),
    draftBatch,
  }
}

function connectionLoading(operator) {
  return !operator.events.length && !operator.isTerminal && operator.status !== 'idle'
}

function Badge({ children, tone = 'neutral' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

function InlineNotice({ message, tone = 'neutral' }) {
  if (!message) return null
  return <div className={`inline-notice inline-notice-${tone}`}>{message}</div>
}

function Section({ eyebrow, title, description, actions, children, className = '' }) {
  return (
    <section className={`panel section-panel ${className}`.trim()}>
      <div className="section-head">
        <div>
          {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
          <h2>{title}</h2>
          {description ? <p className="section-copy">{description}</p> : null}
        </div>
        {actions ? <div className="section-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  )
}

function MetricGrid({ items, className = '' }) {
  return (
    <div className={`metric-grid ${className}`.trim()}>
      {items.map((item) => (
        <article className="metric-card" key={item.label}>
          <div className="metric-label">{item.label}</div>
          <div className="metric-value">{item.value}</div>
          {item.note ? <div className="metric-note">{item.note}</div> : null}
        </article>
      ))}
    </div>
  )
}

function DataState({ error, loading, empty, children, loadingLabel = 'Loading workspace state...', emptyLabel = 'No data yet.', emptyDetail = '' }) {
  if (loading) {
    return (
      <div className="empty-state panel">
        <div className="empty-state-title">{loadingLabel}</div>
        <div className="empty-state-note">Waiting for the backend to hydrate the operator console.</div>
      </div>
    )
  }
  if (error) {
    return (
      <div className="empty-state panel error-state">
        <div className="empty-state-title">Backend error</div>
        <div className="empty-state-note">{error}</div>
      </div>
    )
  }
  if (empty) {
    return (
      <div className="empty-state panel">
        <div className="empty-state-title">{emptyLabel}</div>
        {emptyDetail ? <div className="empty-state-note">{emptyDetail}</div> : null}
      </div>
    )
  }
  return children
}
function OperatorRail({ operator }) {
  return (
    <section className="operator-rail panel">
      <div>
        <div className="eyebrow">Live Operator State</div>
        <h2>{operator.isRunning ? `${operator.runType} / ${STAGE_LABELS[operator.stage] || operator.stage}` : 'No active run'}</h2>
        <p>{operator.latestMessage}</p>
      </div>
      <div className="operator-rail-meta">
        <div className="operator-meta-item"><span>Stream</span><strong>{operator.streamHealth}</strong></div>
        <div className="operator-meta-item"><span>Status</span><strong>{operator.status}</strong></div>
        <div className="operator-meta-item wide"><span>Current target</span><strong>{operator.currentTitle}</strong></div>
        <div className="operator-meta-item"><span>Elapsed</span><strong>{operator.elapsed}</strong></div>
        <div className="operator-meta-item"><span>Last update</span><strong>{operator.lastSeen}</strong></div>
        <div className="operator-meta-item wide"><span>Model</span><strong>{operator.modelBadge}</strong></div>
      </div>
      <div className="operator-rail-badges">
        <Badge tone={toneForStream(operator.streamHealth)}>{operator.streamHealth}</Badge>
        <Badge tone={toneFor(operator.status)}>{operator.status}</Badge>
      </div>
    </section>
  )
}

function CurrentProcessPanel({ operator, compact = false }) {
  const discoveryProgress = operator.counters.discoveryBoardsTotal
    ? `${formatNumber(operator.counters.discoveryBoardsCompleted)} / ${formatNumber(operator.counters.discoveryBoardsTotal)}`
    : '-'
  const draftBatch = operator.draftBatch || {}
  const draftBatchSize = safeNumber(draftBatch.member_count)
  const draftBatchProgress = draftBatchSize
    ? `${formatNumber(draftBatch.completed_count || 0)} / ${formatNumber(draftBatch.member_count || 0)}`
    : '-'
  const activeDraftTabs = safeNumber(draftBatch.active_worker_count || draftBatch.active_count)
  const draftBatchNote = describeDraftBatch(draftBatch, operator.readyThreshold)
  const counters = [
    { label: 'Discovered', value: formatNumber(operator.counters.discovered), note: 'jobs retained in workspace' },
    { label: 'Screened Out', value: formatNumber(operator.counters.screenedOut), note: 'rejected during screening' },
    { label: 'Evaluated', value: formatNumber(operator.counters.evaluated), note: 'application records created' },
    { label: 'Drafted', value: formatNumber(operator.counters.drafted), note: 'artifacts generated' },
    { label: 'Ready', value: formatNumber(operator.counters.readyToApply), note: 'actually ready to apply' },
    { label: 'Blocked', value: formatNumber(operator.counters.blockedByQuestions), note: 'waiting on manual answers' },
    { label: 'Submitted', value: formatNumber(operator.counters.submitted), note: 'successful submissions' },
    { label: 'Failed', value: formatNumber(operator.counters.failed), note: 'run failures' },
  ]

  return (
    <article className={`process-panel panel ${compact ? 'compact' : ''}`.trim()}>
      <div className="process-head">
        <div>
          <div className="eyebrow">Current Process</div>
          <h3>{operator.isRunning ? `${operator.runType} / ${STAGE_LABELS[operator.stage] || operator.stage}` : 'No active run'}</h3>
          <p>{operator.latestMessage}</p>
        </div>
        <div className="process-badges">
          <Badge tone={toneForStream(operator.streamHealth)}>{operator.streamHealth}</Badge>
          <Badge tone={toneFor(operator.status)}>{operator.status}</Badge>
        </div>
      </div>
      <div className="process-meta-grid">
        <div><span>Company / role</span><strong>{operator.currentTitle}</strong></div>
        <div><span>Stage</span><strong>{STAGE_LABELS[operator.stage] || operator.stage}</strong></div>
        <div><span>Elapsed</span><strong>{operator.elapsed}</strong></div>
        <div><span>Last update</span><strong>{operator.lastSeen}</strong></div>
        <div><span>Discovery boards</span><strong>{discoveryProgress}</strong></div>
        <div><span>Draft batch</span><strong>{draftBatchProgress}</strong></div>
        <div><span>Active tabs</span><strong>{draftBatchSize ? formatNumber(activeDraftTabs) : '-'}</strong></div>
        <div><span>Temp chat</span><strong>{operator.temporaryChatStatus || '-'}</strong></div>
        <div><span>Model role</span><strong>{operator.modelRole || '-'}</strong></div>
        <div><span>Model profile</span><strong>{operator.modelProfile || '-'}</strong></div>
      </div>
      <div className="stage-rail">
        {operator.stageTrail.map((item) => (
          <div key={item.key} className={`stage-chip ${item.active ? 'active' : item.done ? 'done' : 'pending'}`.trim()}>
            <span>{item.label}</span>
          </div>
        ))}
      </div>
      <MetricGrid items={compact ? counters.slice(0, 5) : counters} className="process-metrics" />
      {operator.warningNotice ? <InlineNotice message={operator.warningNotice} tone="danger" /> : null}
      {operator.latestError ? <InlineNotice message={operator.latestError} tone="danger" /> : null}
      {draftBatchSize ? <InlineNotice message={`Draft batch: ${draftBatchNote}`} tone={toneFor(draftBatch.status || draftBatch.handoff_status)} /> : null}
      {operator.temporaryChatStatus && operator.temporaryChatStatus !== '-' ? <InlineNotice message={`ChatGPT temp chat: ${operator.temporaryChatStatus}${operator.temporaryChatCheckedAt ? ` (${operator.temporaryChatCheckedAt})` : ''}.`} tone={operator.temporaryChatStatus === 'enabled' ? 'success' : 'warning'} /> : null}
      {operator.latestEventMeta.length ? (
        <div className="event-chips">
          {operator.latestEventMeta.map((chip) => (
            <Badge key={`${chip.label}-${chip.value}`} tone="neutral">{chip.label}: {chip.value}</Badge>
          ))}
        </div>
      ) : null}
    </article>
  )
}

function ActivityFeed({ events }) {
  return (
    <div className="activity-feed">
      {events.map((event) => {
        const chips = summarizeEventPayload(event.payload)
        return (
          <article className="activity-item" key={event.event_id}>
            <div className="activity-meta">
              <Badge tone={toneFor(event.status)}>{badgeText(event.status)}</Badge>
              <Badge tone="neutral">{event.phase || event.stage || 'event'}</Badge>
              <span>{event.event_type}</span>
              <span>{formatDate(event.created_at)}</span>
            </div>
            <strong>{event.message}</strong>
            <div className="activity-subtle">{compactList([event.company, event.role, event.source], 3) || 'System'}</div>
            {event.trace_ref ? <div className="detail-line">Trace Summary: {event.trace_ref}</div> : null}
            {event.error?.message ? <div className="detail-line">{event.error.message}</div> : null}
            {chips.length ? <div className="event-chips">{chips.map((chip) => <Badge key={`${event.event_id}-${chip.label}`} tone="neutral">{chip.label}: {chip.value}</Badge>)}</div> : null}
          </article>
        )
      })}
    </div>
  )
}

function LiveTimelineSection({ operator, live, eyebrow, title, description }) {
  const [traceState, setTraceState] = useState({ ref: '', loading: false, error: '', payload: null })
  const [selectedEventId, setSelectedEventId] = useState('')

  async function openTrace(event) {
    if (!event?.trace_ref) return
    setSelectedEventId(event.event_id || '')
    setTraceState({ ref: event.trace_ref, loading: true, error: '', payload: null })
    try {
      const payload = await requestJson(`/api/live/traces?ref=${encodeURIComponent(event.trace_ref)}`)
      setTraceState({ ref: event.trace_ref, loading: false, error: '', payload })
    } catch (err) {
      setTraceState({ ref: event.trace_ref, loading: false, error: err instanceof Error ? err.message : String(err), payload: null })
    }
  }

  const selectedEvent = operator.eventsDescending.find((event) => event.event_id === selectedEventId) || null

  return (
    <Section eyebrow={eyebrow} title={title} description={description}>
      <div className="live-timeline-grid">
        <DataState error={live.error} loading={!operator.events.length && connectionLoading(operator)} empty={!operator.events.length} emptyLabel="No run events yet." emptyDetail="Start discovery or a full run to populate the live operator timeline.">
          <div className="activity-feed">
            {operator.eventsDescending.slice(0, 24).map((event) => {
              const chips = summarizeEventPayload(event.payload)
              const active = selectedEventId === event.event_id
              return (
                <article className={`activity-item ${active ? 'activity-item-active' : ''}`.trim()} key={event.event_id}>
                  <div className="activity-meta">
                    <Badge tone={toneFor(event.status)}>{badgeText(event.status)}</Badge>
                    <Badge tone="neutral">{event.phase || event.stage || 'event'}</Badge>
                    <span>{event.event_type}</span>
                    <span>{formatDate(event.created_at)}</span>
                  </div>
                  <strong>{event.message}</strong>
                  <div className="activity-subtle">{compactList([event.company, event.role, event.source], 3) || 'System'}</div>
                  {event.error?.message ? <div className="detail-line">{event.error.message}</div> : null}
                  {chips.length ? <div className="event-chips">{chips.map((chip) => <Badge key={`${event.event_id}-${chip.label}`} tone="neutral">{chip.label}: {chip.value}</Badge>)}</div> : null}
                  <div className="form-actions">
                    {event.trace_ref ? <button className="button button-ghost" type="button" onClick={() => openTrace(event)}>Open Summary</button> : null}
                    {event.artifact_paths?.length ? <div className="detail-line">{formatNumber(event.artifact_paths.length)} artifact link(s) recorded</div> : null}
                  </div>
                </article>
              )
            })}
          </div>
        </DataState>
        <article className="subpanel trace-panel">
          <div className="eyebrow">Trace Summary</div>
          <h3>{selectedEvent ? selectedEvent.message : 'Select a traced event'}</h3>
          <div className="detail-line">
            {selectedEvent?.trace_ref || 'Choose any event with a trace ref to inspect the redacted trace summary for that step.'}
          </div>
          {traceState.loading ? <div className="detail-line">Loading summary…</div> : null}
          {traceState.error ? <InlineNotice message={traceState.error} tone="danger" /> : null}
          {traceState.payload ? <pre className="report-block trace-block">{JSON.stringify(traceState.payload.payload || traceState.payload, null, 2)}</pre> : null}
        </article>
      </div>
    </Section>
  )
}

function JobsTable({ rows, onApply }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr><th>Company</th><th>Role</th><th>State</th><th>Progress</th><th>Blockers</th><th>Updated</th><th>Action</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const stateLabel = row.submission_status || row.application_status || row.workflow_state || 'pending'
            const blockers = Array.isArray(row.blockers) ? row.blockers : []
            const updatedAt = row.submitted_at || row.previewed_at || row.evaluated_at || row.discovered_at
            const progressLabel = row.submission_status ? row.submission_status : row.preview_ready ? 'preview_ready' : row.submit_ready ? 'ready_for_submit' : row.workflow_state || 'pending'
            return (
              <tr key={`${row.job_id}-${row.application_id || 'job'}`}>
                <td><strong>{row.company}</strong><div className="cell-meta">{row.source || '-'}</div></td>
                <td><div>{row.role}</div><div className="cell-meta">{row.location || '-'}</div></td>
                <td><Badge tone={toneFor(stateLabel)}>{stateLabel}</Badge></td>
                <td><div>{progressLabel}</div><div className="cell-meta">{row.preview_ready ? 'preview generated' : row.submit_ready ? 'ready to submit' : row.workflow_state || '-'}</div></td>
                <td>{blockers.length ? <div className="cell-meta">{compactList(blockers, 2)}</div> : <span className="cell-meta">-</span>}</td>
                <td>{formatDate(updatedAt)}</td>
                <td>{onApply && row.application_id ? <button className="button button-ghost" type="button" onClick={() => onApply(row)}>Apply</button> : <span className="cell-meta">-</span>}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function FindingsList({ items }) {
  return <div className="findings-list">{items.map((item, index) => <article className="finding-card" key={`${item.key || item.summary}-${index}`}><div className="activity-meta"><Badge tone={toneFor(item.status)}>{item.status}</Badge><span>{item.key}</span></div><strong>{item.summary}</strong>{item.detail ? <p>{item.detail}</p> : null}{item.hint ? <div className="detail-line">{item.hint}</div> : null}</article>)}</div>
}

function SourceHealthPanel({ operator }) {
  const sourceCards = operator.sourceMetrics.length
    ? operator.sourceMetrics.map(([source, metrics]) => ({
      label: source,
      value: `${formatNumber(metrics?.jobs_discovered || 0)} / ${formatNumber(metrics?.eligible_jobs || 0)}`,
      note: `boards ${formatNumber(metrics?.boards_scanned || 0)} scanned · ${formatNumber(metrics?.errors || 0)} errors`,
    }))
    : operator.sourceMix.length
      ? operator.sourceMix.map(([source, count]) => ({ label: source, value: formatNumber(count), note: 'discovered jobs' }))
    : PORTAL_SOURCE_OPTIONS.map((source) => ({ label: source.label, value: '0', note: 'no jobs discovered' }))
  const findings = [
    ...operator.zeroResultSources.map((source) => ({
      key: `zero-result:${source}`,
      status: 'warning',
      summary: `${source} has not contributed any jobs in the current workspace.`,
      detail: 'The scheduler now scans it explicitly, so a zero count usually means the board scope or source runtime is failing.',
    })),
    ...operator.discoveryErrors.map(([source, count]) => ({
      key: `discovery-error:${source}`,
      status: 'blocked',
      summary: `${source} reported ${formatNumber(count)} discovery error(s).`,
      detail: 'Inspect the live timeline and trace summaries to find the exact failing board or runtime dependency.',
    })),
    ...operator.sourceWarnings.map((warning, index) => ({
      key: `source-warning:${index}`,
      status: 'warning',
      summary: warning,
    })),
    ...operator.screenedOutReasons.slice(0, 6).map(([reason, count]) => ({
      key: `screened-out:${reason}`,
      status: 'neutral',
      summary: `${formatNumber(count)} job(s) screened out as ${reason}.`,
    })),
  ]

  return (
    <Section eyebrow="Source Coverage" title="Discovery Truth" description="Every enabled source is tracked independently so Greenhouse cannot hide Lever or Ashby failures behind aggregate counts." className="subsection-panel">
      <MetricGrid items={sourceCards} />
      {findings.length ? <FindingsList items={findings} /> : null}
    </Section>
  )
}
function DashboardPage({ operator, live }) {
  const dashboard = usePolledJson('/api/dashboard', 7000)
  const counts = dashboard.data?.snapshot?.counts || {}
  const auto = dashboard.data?.autonomous || {}
  const jobsTable = dashboard.data?.jobs_table?.items || []
  const [notice, setNotice] = useState('')
  const [resetting, setResetting] = useState(false)

  async function startDiscover() {
    await requestJson('/api/discover', { method: 'POST' })
    await Promise.allSettled([live.refresh(), dashboard.refresh()])
  }

  async function startAutonomous() {
    await requestJson('/api/autonomous/run', { method: 'POST' })
    await Promise.allSettled([live.refresh(), dashboard.refresh()])
  }

  async function applyNow(row) {
    if (!row?.application_id) return
    await requestJson('/api/review/action', {
      method: 'POST',
      body: JSON.stringify({ application_id: row.application_id, action: 'approve' }),
    })
    await Promise.allSettled([dashboard.refresh(), live.refresh()])
  }

  async function resetOperationalData() {
    setResetting(true)
    try {
      const result = await requestJson('/api/workspace/reset-operational', { method: 'POST' })
      const deleted = result?.deleted || {}
      setNotice(`Reset complete. Cleared ${deleted.applications || 0} applications, ${deleted.jobs || 0} job files, ${deleted.runs || 0} runs.`)
      await Promise.allSettled([dashboard.refresh(), live.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    } finally {
      setResetting(false)
    }
  }

  const summaryCards = [
    { label: 'Inbox', value: formatNumber(counts.inbox ?? 0), note: 'jobs in workspace' },
    { label: 'Applications', value: formatNumber(operator.counters.evaluated || counts.applications || 0), note: 'tracked application records' },
    { label: 'Queue Depth', value: formatNumber(auto.queue_depth ?? operator.queue.depth), note: 'active submission records' },
    { label: 'Blocked', value: formatNumber(operator.counters.blockedByQuestions || auto.blocked_by_questions || auto.blocked_applications || operator.queue.blocked), note: 'manual answers needed' },
    { label: 'Ready', value: formatNumber(operator.counters.readyToApply || auto.ready_to_apply || auto.ready_for_submit || 0), note: `${formatNumber(auto.ready_to_apply_threshold || 5)} threshold` },
    { label: 'Prompts', value: formatNumber(auto.unresolved_prompts ?? operator.queue.pendingQuestions), note: 'manual questions waiting' },
  ]

  const discoveryCards = [
    { label: 'Discovered', value: formatNumber(operator.counters.discovered), note: 'jobs retained in workspace' },
    { label: 'Screened Out', value: formatNumber(operator.counters.screenedOut), note: 'rejected during screening' },
    { label: 'Evaluated', value: formatNumber(operator.counters.evaluated), note: 'records created from evaluation' },
    { label: 'Drafted', value: formatNumber(operator.counters.drafted), note: 'artifacts generated' },
    { label: 'Ready To Apply', value: formatNumber(operator.counters.readyToApply), note: 'actual apply queue' },
    { label: 'Submitted', value: formatNumber(operator.counters.submitted), note: 'successful submissions' },
    { label: 'Blocked By Questions', value: formatNumber(operator.counters.blockedByQuestions), note: 'waiting on operator answers' },
    { label: 'Board Progress', value: operator.counters.discoveryBoardsTotal ? `${formatNumber(operator.counters.discoveryBoardsCompleted)} / ${formatNumber(operator.counters.discoveryBoardsTotal)}` : '-', note: `${formatNumber(operator.counters.discoverySeedPages)} seed pages crawled` },
    { label: 'Hard Rejected', value: formatNumber(operator.counters.deterministicRejects), note: 'title or hard filters' },
    { label: 'Failed', value: formatNumber(operator.counters.failed), note: 'terminal application failures' },
  ]

  return (
    <div className="page-stack">
      <Section eyebrow="Operator Console" title="Find My Job Console" description="Discovery, screening, drafting, review, and submit state now follows the backend run state instead of stale browser state." actions={<><button className="button button-primary" type="button" onClick={startDiscover} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'discover' ? 'Discovery Running' : 'Discover Jobs'}</button><button className="button button-primary" type="button" onClick={startAutonomous} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'autonomous' ? 'Full Run Running' : 'Full Run'}</button><button className="button button-ghost" type="button" onClick={resetOperationalData} disabled={resetting}>{resetting ? 'Resetting' : 'Reset Operational Data'}</button></>}>
        <div className="section-stack">
          <CurrentProcessPanel operator={operator} />
          <InlineNotice message={notice} tone={toneFor(notice)} />
          <MetricGrid items={summaryCards} />
          <Section eyebrow="Scoreboard" title="Discovery Scoreboard" description="Per-run counters from the live state." className="subsection-panel">
            <MetricGrid items={discoveryCards} />
          </Section>
          <SourceHealthPanel operator={operator} />
        </div>
      </Section>

      <LiveTimelineSection operator={operator} live={live} eyebrow="Live Feed" title="Run Timeline" description="Structured live events from discovery through submission, with redacted trace summaries for model calls and pipeline steps." />

      <Section eyebrow="Operator Queue" title="Jobs And Application State" description="Priority queue view showing current stage, blockers, and next action.">
        <DataState error={dashboard.error} loading={dashboard.loading} empty={!jobsTable.length} emptyLabel="No queue rows yet." emptyDetail="Discovery will populate jobs here and the apply pipeline will add submission state.">
          <JobsTable rows={jobsTable.slice(0, 20)} onApply={applyNow} />
        </DataState>
      </Section>
    </div>
  )
}

function SetupPage() {
  const readiness = usePolledJson('/api/setup/readiness', 8000)
  const [message, setMessage] = useState('')
  const [resetting, setResetting] = useState(false)
  const findings = readiness.data?.findings || []
  const profileSurface = readiness.data?.profile_surface || {}
  const profileMode =
    profileSurface.mode === 'local_user_profile'
      ? 'Configured Local Profile'
      : profileSurface.mode === 'advanced_local_overrides'
        ? 'Advanced Local Overrides'
        : 'Sample Mode'
  const activeAdvancedPaths = Array.isArray(profileSurface.active_advanced_paths) ? profileSurface.active_advanced_paths.filter(Boolean) : []

  async function resetOperationalData() {
    setResetting(true)
    try {
      const result = await requestJson('/api/workspace/reset-operational', { method: 'POST' })
      const deleted = result?.deleted || {}
      setMessage(`Reset complete. Cleared ${deleted.applications || 0} applications, ${deleted.submissions || 0} submissions, ${deleted.runs || 0} runs.`)
      await readiness.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="page-stack">
      <Section eyebrow="Readiness" title="Workspace And Launch Checks" description="Config validation, model doctor, and launch checks from the backend release pipeline." actions={<button className="button button-ghost" type="button" onClick={resetOperationalData} disabled={resetting}>{resetting ? 'Resetting...' : 'Reset Operational Data'}</button>}>
        <DataState error={readiness.error} loading={readiness.loading} empty={!readiness.data}>
          <MetricGrid items={[
            { label: 'Overall', value: readiness.data?.overall_status || '-', note: 'combined release signal' },
            { label: 'Config', value: readiness.data?.config_validation?.overall_status || '-', note: 'workspace config' },
            { label: 'Doctor', value: readiness.data?.doctor?.overall_status || '-', note: 'model and runtime' },
            { label: 'Launch', value: readiness.data?.launch_check?.overall_status || '-', note: 'production launch gate' },
            { label: 'Sources', value: formatNumber(Object.keys(readiness.data?.sources || {}).length), note: 'configured source families' },
            { label: 'Submit', value: readiness.data?.automation?.submit_enabled ? 'on' : 'off', note: 'submit toggle' },
            { label: 'Profile Mode', value: profileMode, note: profileSurface.configured ? 'local-only candidate data is active' : 'still using tracked sample data' },
          ]} />
          <div className="panel" style={{ marginTop: '1rem' }}>
            <div className="eyebrow">Local Profile Surface</div>
            <h3>{profileMode}</h3>
            <p className="section-copy">
              {profileSurface.configured
                ? 'The repo is reading local-only candidate data from ignored override paths.'
                : 'The repo is still using tracked sample candidate data. Create a local-only profile before real runs.'}
            </p>
            <p><strong>Local profile file:</strong> <code>{profileSurface.local_path || '.fmj/local-overrides/filefirst/user-profile.yml'}</code></p>
            <p><strong>Local template:</strong> <code>{profileSurface.local_template_path || '.fmj/local-overrides/filefirst/user-profile.template.yml'}</code></p>
            <p><strong>Tracked example:</strong> <code>{profileSurface.public_template_path || 'templates/user-profile.local.example.yml'}</code></p>
            {activeAdvancedPaths.length ? <p><strong>Active advanced overrides:</strong> {activeAdvancedPaths.join(', ')}</p> : null}
          </div>
          <InlineNotice message={message} tone={toneFor(message)} />
          <FindingsList items={findings} />
        </DataState>
      </Section>
    </div>
  )
}
const ROUTING_FAMILIES = [
  {
    id: 'screening',
    label: 'Screening Model',
    description: 'One model for discovery routing, classification, and extraction.',
    note: 'Keeps separate prompts for each task, but updates all three roles together.',
    roles: [
      { role: 'text_router', label: 'Text Router', profilePrefix: 'lmstudio-screen' },
      { role: 'classifier', label: 'Classifier', profilePrefix: 'lmstudio-screen' },
      { role: 'extractor', label: 'Extractor', profilePrefix: 'lmstudio-screen' },
    ],
  },
  {
    id: 'drafting',
    label: 'Legacy Drafting Roles',
    description: 'Optional local writer bindings kept for rollback only. Live document drafting now uses the managed ChatGPT profile.',
    note: 'These roles are no longer part of the primary launch gate when ChatGPT drafting is active.',
    roles: [
      { role: 'writer', label: 'Writer', profilePrefix: 'lmstudio-draft' },
      { role: 'resume_writer', label: 'Resume Writer', profilePrefix: 'lmstudio-draft' },
      { role: 'cover_letter_writer', label: 'Cover Letter Writer', profilePrefix: 'lmstudio-draft' },
    ],
  },
  {
    id: 'qa',
    label: 'Q&A Model',
    description: 'A separate model for application-question answering.',
    note: 'Kept separate because form Q&A tends to need different prompting and lower-variance answers.',
    roles: [
      { role: 'question_answerer', label: 'Question Answerer', profilePrefix: 'lmstudio-draft' },
    ],
  },
]

const LMSTUDIO_DEFAULT_HOST = 'http://127.0.0.1:1234'
const LMSTUDIO_DEFAULT_PROVIDER = 'lmstudio'

const PORTAL_SOURCE_OPTIONS = [
  { id: 'greenhouse', label: 'Greenhouse', note: 'Launch default. Uses the curated Greenhouse board universe plus seed crawling when no boards are pinned.', launchDefault: true, experimental: false },
  { id: 'lever', label: 'Lever', note: 'Experimental. Disabled by default and excluded from autonomous launch runs until you opt in.', launchDefault: false, experimental: true },
  { id: 'ashby', label: 'Ashby', note: 'Experimental. Disabled by default and excluded from autonomous launch runs until you opt in.', launchDefault: false, experimental: true },
]

const MODEL_PROVIDER_OPTIONS = [
  { value: LMSTUDIO_DEFAULT_PROVIDER, label: 'LM Studio' },
]

const MODEL_TRANSPORT_OPTIONS = [
  { value: 'local_http', label: 'Local HTTP' },
]

function applyProviderDefaults(model) {
  return {
    ...model,
    provider: LMSTUDIO_DEFAULT_PROVIDER,
    transport: 'local_http',
    base_url: model?.base_url || LMSTUDIO_DEFAULT_HOST,
    api_key_env: '',
    local: true,
    command: [],
    working_dir: '',
  }
}

function appendQuery(url, params) {
  const query = new URLSearchParams()
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    query.set(key, String(value))
  })
  const suffix = query.toString()
  return suffix ? `${url}?${suffix}` : url
}

function toMultiline(value) {
  return Array.isArray(value) ? value.join('\n') : ''
}

function blankPortalSource(sourceId = 'greenhouse') {
  return { enabled: sourceId === 'greenhouse', boards: [], seed_urls: [], seed_domains: [] }
}

function blankTrackedCompany(source = 'greenhouse') {
  return { name: '', careers_url: '', source, board: '', api: '', enabled: true, notes: '' }
}

function modelCatalogUrl(profile, forceRefresh = false) {
  return appendQuery('/api/settings/models/available', {
    refresh: forceRefresh ? 'true' : undefined,
    profile_name: profile?.name,
    provider: profile?.provider,
    transport: profile?.transport,
    base_url: profile?.base_url,
    api_key_env: profile?.api_key_env,
  })
}

function transportLabel(value) {
  return MODEL_TRANSPORT_OPTIONS.find((option) => option.value === value)?.label || value || '-'
}

function providerLabel(value) {
  return MODEL_PROVIDER_OPTIONS.find((option) => option.value === value)?.label || value || '-'
}

function ProfileInventory({ profiles, checks, onPing, onDelete, loadingState }) {
  return (
    <div className="profile-list">
      {profiles.map((profile) => {
        const check = checks?.[profile.name]
        const pinging = Boolean(loadingState?.[`ping:${profile.name}`])
        const deleting = Boolean(loadingState?.[`delete:${profile.name}`])
        return (
          <article className="profile-card" key={profile.name}>
            <div className="activity-meta">
              <Badge tone={toneFor(profile.status)}>{profile.status}</Badge>
              <span>{profile.role || '-'}</span>
            </div>
            <strong>{profile.name}</strong>
            <div className="detail-line">{providerLabel(profile.provider)} / {profile.model}</div>
            <div className="detail-line">{transportLabel(profile.transport)} {profile.base_url ? `· ${profile.base_url}` : ''}</div>
            {check ? <div className="detail-line">Last ping: {check.classification || (check.ok ? 'ok' : 'failed')} · {formatDate(check.checked_at)}</div> : <div className="detail-line">No model ping recorded yet.</div>}
            <div className="form-actions">
              <button className="button button-ghost" type="button" onClick={() => onPing({ profile_name: profile.name }, `ping:${profile.name}`)} disabled={pinging}>
                {pinging ? 'Pinging...' : 'Ping'}
              </button>
              <button className="button button-ghost" type="button" onClick={() => onDelete(profile.name)} disabled={deleting}>
                {deleting ? 'Removing...' : 'Delete'}
              </button>
            </div>
          </article>
        )
      })}
    </div>
  )
}

function ModelHotSwap({ settings, onSaved, onPing, loadingState = {} }) {
  const runtimeModel = settings?.runtime_model || settings?.local_model || {}
  const roleBindings = settings?.advanced_models?.role_bindings || {}
  const profiles = settings?.advanced_models?.profiles || []
  const profileByName = Object.fromEntries(profiles.map((profile) => [profile.name, profile]))
  const [manualModels, setManualModels] = useState({})
  const [selectedModels, setSelectedModels] = useState({})
  const [saving, setSaving] = useState({})
  const [catalogs, setCatalogs] = useState({})
  const [loadingCatalog, setLoadingCatalog] = useState({})
  const [notice, setNotice] = useState('')

  function profileNameForRole(roleInfo) {
    return roleBindings[roleInfo.role] || `${roleInfo.profilePrefix}-${roleInfo.role.replace(/_/g, '-')}`
  }

  function profileForRole(roleInfo) {
    return applyProviderDefaults(profileByName[profileNameForRole(roleInfo)] || {
      ...runtimeModel,
      name: profileNameForRole(roleInfo),
      role: roleInfo.role,
      provider: runtimeModel.provider || LMSTUDIO_DEFAULT_PROVIDER,
      transport: runtimeModel.transport || 'local_http',
      model: runtimeModel.model || '',
      base_url: runtimeModel.base_url || LMSTUDIO_DEFAULT_HOST,
      api_key_env: runtimeModel.api_key_env || '',
      command: runtimeModel.command || [],
      working_dir: runtimeModel.working_dir || '',
    })
  }

  async function loadAvailableModels(family, forceRefresh = false) {
    const primaryProfile = profileForRole(family.roles[0])
    setLoadingCatalog((prev) => ({ ...prev, [family.id]: true }))
    try {
      const data = await requestJson(modelCatalogUrl(primaryProfile, forceRefresh))
      setCatalogs((prev) => ({ ...prev, [family.id]: data }))
      if (data?.error) {
        setNotice(data.error)
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingCatalog((prev) => ({ ...prev, [family.id]: false }))
    }
  }

  useEffect(() => {
    const refreshCatalogs = () => {
      ROUTING_FAMILIES.forEach((family) => {
        void loadAvailableModels(family)
      })
    }
    refreshCatalogs()
    const intervalId = window.setInterval(refreshCatalogs, 30_000)
    return () => window.clearInterval(intervalId)
  }, [settings])

  function currentModelForRole(role) {
    const profileName = roleBindings[role]
    if (!profileName) return runtimeModel?.model || ''
    const profile = profiles.find((item) => item.name === profileName)
    return profile?.model || runtimeModel?.model || ''
  }

  function currentModelForFamily(family) {
    const unique = [...new Set(family.roles.map((roleInfo) => currentModelForRole(roleInfo.role)).filter(Boolean))]
    return unique.length === 1 ? unique[0] : ''
  }

  function selectedModelForFamily(family) {
    return selectedModels[family.id] ?? currentModelForFamily(family)
  }

  function manualModelForFamily(family) {
    return manualModels[family.id] ?? selectedModelForFamily(family) ?? ''
  }

  function modelOptionLabel(model) {
    return model?.label || (model?.name && model?.name !== model?.id ? `${model.name} (${model.id})` : model?.id || '')
  }

  function visibleModels(models) {
    return (models || []).filter((model) => {
      const modelId = String(model?.id || '').toLowerCase()
      return !modelId.includes('embed') && !modelId.includes('embedding')
    })
  }

  async function saveRoleModel(roleInfo, newModelId) {
    const existing = profileForRole(roleInfo)
    await requestJson('/api/settings/models', {
      method: 'POST',
        body: JSON.stringify({
          name: profileNameForRole(roleInfo),
          role: roleInfo.role,
          provider: existing?.provider || runtimeModel?.provider || LMSTUDIO_DEFAULT_PROVIDER,
          model: newModelId,
          transport: existing?.transport || runtimeModel?.transport || 'local_http',
          base_url: existing?.base_url || runtimeModel?.base_url || LMSTUDIO_DEFAULT_HOST,
          api_key_env: existing?.api_key_env || runtimeModel?.api_key_env || '',
        temperature: existing?.temperature ?? (roleInfo.profilePrefix.includes('draft') ? 0.7 : 0.0),
        max_tokens: existing?.max_tokens || runtimeModel?.max_tokens || 8192,
        preferred_context_window: existing?.preferred_context_window || runtimeModel?.preferred_context_window || 131072,
        supports_structured_output: existing?.supports_structured_output ?? true,
        fallback_chain: existing?.fallback_chain || [],
        policy_tags: existing?.policy_tags || [],
        local: Boolean(existing?.local),
        command: existing?.command || [],
        working_dir: existing?.working_dir || '',
      }),
    })
  }

  function familyStatusLine(family) {
    const primaryProfile = profileForRole(family.roles[0])
    const familyCatalog = catalogs[family.id] || { models: [] }
    const availableModels = visibleModels(familyCatalog.models || [])
    const bound = family.roles.map((roleInfo) => ({
      label: roleInfo.label,
      model: currentModelForRole(roleInfo.role),
    }))
    const unique = [...new Set(bound.map((item) => item.model).filter(Boolean))]
    if (unique.length <= 1) {
      const modelId = unique[0]
      const entry = availableModels.find((item) => item.id === modelId)
      if (entry) {
        return `${providerLabel(primaryProfile.provider)} · ${transportLabel(primaryProfile.transport)} · ctx ${formatNumber(entry.context_length || 0)} · ${entry.id}`
      }
      return `${providerLabel(primaryProfile.provider)} · ${transportLabel(primaryProfile.transport)} · ${modelId || 'No family-wide model selected yet.'}`
    }
    return `Mixed current bindings: ${bound.map((item) => `${item.label}=${item.model || '-'}`).join(' · ')}`
  }

  async function swapFamilyModel(family, newModelId) {
    const key = family.id
    const normalizedModelId = String(newModelId || '').trim()
    if (!normalizedModelId) {
      setNotice(`Enter a model id for ${family.label}.`)
      return
    }
    setSelectedModels((prev) => ({ ...prev, [key]: normalizedModelId }))
    setSaving((prev) => ({ ...prev, [key]: true }))
    setNotice('')
    try {
      for (const roleInfo of family.roles) {
        await saveRoleModel(roleInfo, normalizedModelId)
      }
      setManualModels((prev) => ({ ...prev, [key]: normalizedModelId }))
      setNotice(`${family.label} switched to ${normalizedModelId}`)
      if (onSaved) await onSaved()
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    } finally {
      setSelectedModels((prev) => {
        const next = { ...prev }
        delete next[key]
        return next
      })
      setSaving((prev) => ({ ...prev, [key]: false }))
    }
  }

  async function applyManualModel(event, family) {
    event.preventDefault()
    await swapFamilyModel(family, manualModelForFamily(family))
  }

  return (
    <div className="hotswap-panel">
      <InlineNotice message={notice} tone={toneFor(notice)} />
      <div className="hotswap-grid">
        {ROUTING_FAMILIES.map((family) => {
          const current = selectedModelForFamily(family)
          const familyCatalog = catalogs[family.id] || { models: [] }
          const availableModels = visibleModels(familyCatalog.models || [])
          const freeModels = availableModels.filter((model) => model.tier === 'free')
          const nonFreeModels = availableModels.filter((model) => model.tier !== 'free')
          const primaryProfile = profileForRole(family.roles[0])
          const isSaving = saving[family.id]
          const isPinging = Boolean(loadingState?.[`family:${family.id}`])
          const currentModelLabel = currentModelForFamily(family) || 'mixed bindings'
          return (
            <article key={family.id} className="subpanel hotswap-group">
              <div className="eyebrow">{family.label}</div>
              <p className="detail-line" style={{ marginBottom: 12 }}>{family.description}</p>
              <div className="hotswap-role">
                <div className="activity-meta">
                  <Badge tone={familyCatalog.key_scoped ? 'success' : familyCatalog.api_key_configured ? 'warning' : 'neutral'}>{providerLabel(primaryProfile.provider)}</Badge>
                  <span>{transportLabel(primaryProfile.transport)}</span>
                  <span>{formatNumber(availableModels.length)} models</span>
                </div>
                <label>
                  <span className="hotswap-role-label">{family.roles.map((roleInfo) => roleInfo.label).join(', ')}</span>
                  <select value={current} disabled={isSaving} onChange={(event) => swapFamilyModel(family, event.target.value)}>
                    <option value="">{current ? 'Select a live model' : 'Select a family-wide model'}</option>
                    {freeModels.length ? <optgroup label="Free Models">{freeModels.map((model) => <option key={model.id} value={model.id}>{modelOptionLabel(model)}</option>)}</optgroup> : null}
                    {nonFreeModels.length ? <optgroup label="Available Models">{nonFreeModels.map((model) => <option key={model.id} value={model.id}>{modelOptionLabel(model)}</option>)}</optgroup> : null}
                    {current && !availableModels.find((model) => model.id === current) ? <option value={current}>{current} (custom)</option> : null}
                  </select>
                </label>
                <div className="hotswap-current-model">Current model: <strong>{currentModelLabel}</strong></div>
                <form className="hotswap-manual" onSubmit={(event) => applyManualModel(event, family)}>
                  <input
                    list={`model-catalog-${family.id}`}
                    value={manualModelForFamily(family)}
                    onChange={(event) => setManualModels((prev) => ({ ...prev, [family.id]: event.target.value }))}
                    placeholder="Paste a model id"
                    disabled={isSaving}
                  />
                  <button className="button button-ghost" type="submit" disabled={isSaving}>Apply ID</button>
                </form>
                <div className="detail-line">{family.note}</div>
                <div className="detail-line">{familyStatusLine(family)}</div>
                {familyCatalog.note ? <div className="detail-line">{familyCatalog.note}</div> : null}
                <div className="form-actions">
                  <button className="button button-ghost" type="button" onClick={() => loadAvailableModels(family, true)} disabled={Boolean(loadingCatalog[family.id])}>
                    {loadingCatalog[family.id] ? 'Refreshing...' : 'Refresh Catalog'}
                  </button>
                  <button className="button button-ghost" type="button" onClick={() => onPing({ ...primaryProfile, name: profileNameForRole(family.roles[0]), role: family.roles[0].role }, `family:${family.id}`)} disabled={isPinging}>
                    {isPinging ? 'Pinging...' : 'Ping Family'}
                  </button>
                </div>
                {isSaving ? <span className="hotswap-saving">saving...</span> : null}
              </div>
              <datalist id={`model-catalog-${family.id}`}>
                {availableModels.map((model) => <option key={model.id} value={model.id}>{modelOptionLabel(model)}</option>)}
              </datalist>
            </article>
          )
        })}
      </div>
    </div>
  )
}

function SettingsPage() {
  const settings = usePolledJson('/api/settings', 8000)
  const [message, setMessage] = useState('')
  const [savingState, setSavingState] = useState({})
  const [dirtyState, setDirtyState] = useState({ autonomous: false, chatgpt: false, portals: false, runtime: false, profile: false })
  const [autonomousForm, setAutonomousForm] = useState(null)
  const [chatgptForm, setChatgptForm] = useState(null)
  const [portalForm, setPortalForm] = useState(null)
  const [runtimeModelForm, setRuntimeModelForm] = useState(null)
  const [runtimeCatalog, setRuntimeCatalog] = useState({ models: [], count: 0, source: 'unconfigured' })
  const [profileForm, setProfileForm] = useState(applyProviderDefaults({
    name: '', role: 'writer', provider: LMSTUDIO_DEFAULT_PROVIDER, transport: 'local_http', model: '', base_url: LMSTUDIO_DEFAULT_HOST, api_key_env: '', temperature: 0.2, max_tokens: 8192, preferred_context_window: 131072, supports_structured_output: true, fallback_chain_text: '', policy_tags_text: '', local: true, command_text: '', working_dir: '',
  }))

  function markDirty(key) {
    setDirtyState((prev) => ({ ...prev, [key]: true }))
  }

  useEffect(() => {
    if (settings.data?.autonomous && !dirtyState.autonomous) setAutonomousForm(settings.data.autonomous)
    if (settings.data?.chatgpt_drafting && !dirtyState.chatgpt) {
      setChatgptForm({
        enabled: Boolean(settings.data.chatgpt_drafting.enabled),
        gpt_url: settings.data.chatgpt_drafting.gpt_url || '',
        completion_start_marker: settings.data.chatgpt_drafting.completion_start_marker || '[[PDF_OUTPUT_READY]]',
        completion_end_marker: settings.data.chatgpt_drafting.completion_end_marker || '[[PDF_OUTPUT_COMPLETE]]',
        profile_dir: settings.data.chatgpt_drafting.browser?.profile_dir || '.fmj/browser/chatgpt-profile',
        downloads_dir: settings.data.chatgpt_drafting.browser?.downloads_dir || '.fmj/runtime/chatgpt-downloads',
        browser_mode: settings.data.chatgpt_drafting.browser?.browser_mode || 'attached',
        browser_cdp_url: settings.data.chatgpt_drafting.browser?.browser_cdp_url || 'http://127.0.0.1:9333',
        launch_if_missing: settings.data.chatgpt_drafting.browser?.launch_if_missing ?? true,
        use_temporary_chat: settings.data.chatgpt_drafting.use_temporary_chat ?? false,
        timeout_seconds: settings.data.chatgpt_drafting.timeout_seconds || 240,
        prompt_submit_delay_ms: settings.data.chatgpt_drafting.prompt_submit_delay_ms || 300,
        download_timeout_seconds: settings.data.chatgpt_drafting.download_timeout_seconds || 120,
        max_parallel_jobs: settings.data.chatgpt_drafting.max_parallel_jobs || 10,
        make_default: true,
      })
    }
    if (settings.data?.portals && !dirtyState.portals) {
      const sources = {}
      PORTAL_SOURCE_OPTIONS.forEach((source) => {
        sources[source.id] = settings.data.portals.sources?.[source.id] || blankPortalSource(source.id)
      })
      setPortalForm({
        sources,
        tracked_companies: Array.isArray(settings.data.tracked_companies) ? settings.data.tracked_companies : (settings.data.portals.tracked_companies || []),
      })
    }
    if ((settings.data?.runtime_model || settings.data?.local_model) && !dirtyState.runtime) {
      setRuntimeModelForm(applyProviderDefaults(settings.data.runtime_model || settings.data.local_model))
    }
  }, [dirtyState.autonomous, dirtyState.chatgpt, dirtyState.portals, dirtyState.runtime, settings.data])

  const advancedProfiles = settings.data?.advanced_models?.profiles || []
  const runtimeChecks = settings.data?.last_model_checks || {}
  const modelStrategy = settings.data?.model_strategy || {}
  const readinessFindings = settings.data?.readiness?.findings || []

  async function withSaving(key, fn) {
    setSavingState((prev) => ({ ...prev, [key]: true }))
    try {
      return await fn()
    } finally {
      setSavingState((prev) => ({ ...prev, [key]: false }))
    }
  }

  async function loadRuntimeCatalog(forceRefresh = false, nextRuntimeModel = runtimeModelForm) {
    if (!nextRuntimeModel) return
    try {
      const data = await requestJson(modelCatalogUrl({ name: 'runtime-model', ...nextRuntimeModel }, forceRefresh))
      setRuntimeCatalog(data)
      if (data?.error) setMessage(data.error)
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  useEffect(() => {
    if (runtimeModelForm) {
      void loadRuntimeCatalog(false, runtimeModelForm)
    }
  }, [runtimeModelForm?.provider, runtimeModelForm?.transport, runtimeModelForm?.base_url, runtimeModelForm?.api_key_env])

  async function pingModel(payload, loadingKey = 'ping') {
    return withSaving(loadingKey, async () => {
      const result = await requestJson('/api/settings/models/ping', { method: 'POST', body: JSON.stringify(payload) })
      setMessage(result.ok ? `Ping ok for ${result.profile || result.model}.` : `Ping failed for ${result.profile || result.model}: ${result.error || result.classification}`)
      await settings.refresh()
      return result
    })
  }

  async function saveAutonomous(event) {
    event.preventDefault()
    if (!autonomousForm) return
    try {
      await withSaving('autonomous', () => requestJson('/api/settings/autonomous', { method: 'POST', body: JSON.stringify(autonomousForm) }))
      setMessage('Automation settings saved.')
      setDirtyState((prev) => ({ ...prev, autonomous: false }))
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function saveChatgptDrafting(event) {
    event.preventDefault()
    if (!chatgptForm) return
    try {
      await withSaving('chatgpt', () => requestJson('/api/settings/chatgpt-drafting', { method: 'POST', body: JSON.stringify(chatgptForm) }))
      setMessage('ChatGPT drafting settings saved.')
      setDirtyState((prev) => ({ ...prev, chatgpt: false }))
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function launchChatgptBrowser() {
    try {
      await withSaving('chatgptLaunch', () => requestJson('/api/chatgpt-drafting/browser/launch', {
        method: 'POST',
        body: JSON.stringify({ close_existing: true, start_blank: true }),
      }))
      setMessage('Managed ChatGPT browser launch triggered.')
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function testChatgptDrafting() {
    try {
      await withSaving('chatgptTest', () => requestJson('/api/chatgpt-drafting/test', { method: 'POST', body: JSON.stringify({}) }))
      setMessage('ChatGPT drafting test completed.')
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function savePortals(event) {
    event.preventDefault()
    if (!portalForm) return
    try {
      const payload = {
        sources: Object.fromEntries(PORTAL_SOURCE_OPTIONS.map((source) => [source.id, {
          enabled: Boolean(portalForm.sources?.[source.id]?.enabled),
          boards: toMultiline(portalForm.sources?.[source.id]?.boards).split('\n').map((item) => item.trim()).filter(Boolean),
          seed_urls: toMultiline(portalForm.sources?.[source.id]?.seed_urls).split('\n').map((item) => item.trim()).filter(Boolean),
          seed_domains: toMultiline(portalForm.sources?.[source.id]?.seed_domains).split('\n').map((item) => item.trim()).filter(Boolean),
        }])),
        tracked_companies: (portalForm.tracked_companies || []).map((item) => ({
          ...item,
          name: String(item.name || '').trim(),
          careers_url: String(item.careers_url || '').trim(),
          source: String(item.source || '').trim(),
          board: String(item.board || '').trim(),
          api: String(item.api || '').trim(),
          notes: String(item.notes || '').trim(),
        })).filter((item) => item.name),
      }
      await withSaving('portals', () => requestJson('/api/settings/portals', { method: 'PUT', body: JSON.stringify(payload) }))
      setMessage('Portal settings saved.')
      setDirtyState((prev) => ({ ...prev, portals: false }))
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function saveRuntimeModel(event) {
    event.preventDefault()
    if (!runtimeModelForm) return
    try {
      await withSaving('runtime', () => requestJson('/api/settings/runtime-model', {
        method: 'PUT',
        body: JSON.stringify({
          provider: LMSTUDIO_DEFAULT_PROVIDER,
          transport: 'local_http',
          model: runtimeModelForm.model,
          base_url: runtimeModelForm.base_url,
          temperature: Number(runtimeModelForm.temperature || 0.2),
          max_tokens: Number(runtimeModelForm.max_tokens || 8192),
          preferred_context_window: Number(runtimeModelForm.preferred_context_window || 131072),
          local: true,
          command: [],
          working_dir: '',
        }),
      }))
      setMessage('Runtime model settings saved.')
      setDirtyState((prev) => ({ ...prev, runtime: false }))
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function saveProfile(event) {
    event.preventDefault()
    try {
      await withSaving('profile', () => requestJson('/api/settings/models', {
        method: 'POST',
        body: JSON.stringify({
          name: profileForm.name,
          role: profileForm.role,
          provider: LMSTUDIO_DEFAULT_PROVIDER,
          transport: 'local_http',
          model: profileForm.model,
          base_url: profileForm.base_url,
          temperature: Number(profileForm.temperature || 0.2),
          max_tokens: Number(profileForm.max_tokens || 8192),
          preferred_context_window: Number(profileForm.preferred_context_window || 131072),
          supports_structured_output: Boolean(profileForm.supports_structured_output),
          fallback_chain: String(profileForm.fallback_chain_text || '').split(',').map((item) => item.trim()).filter(Boolean),
          policy_tags: String(profileForm.policy_tags_text || '').split(',').map((item) => item.trim()).filter(Boolean),
          local: true,
          command: [],
          working_dir: '',
        }),
      }))
      setMessage(`Model profile ${profileForm.name} saved.`)
      setDirtyState((prev) => ({ ...prev, profile: false }))
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function deleteProfile(name) {
    try {
      await withSaving(`delete:${name}`, () => requestJson('/api/settings/models', { method: 'DELETE', body: JSON.stringify({ name }) }))
      setMessage(`Model profile ${name} deleted.`)
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function installRecommended() {
    try {
      await withSaving('recommended', () => requestJson('/api/settings/models/recommended', { method: 'POST' }))
      setMessage('Recommended split profiles installed.')
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  async function regenerateDossier() {
    try {
      await withSaving('dossier', () => requestJson('/api/profile/dossier/regenerate', { method: 'POST' }))
      setMessage('Candidate dossier regenerated.')
      await settings.refresh()
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="page-stack">
      <Section eyebrow="Launch Readiness" title="Control Center" description="One control plane for launch scope, runtime models, routing families, and health checks.">
        <DataState error={settings.error} loading={settings.loading} empty={!settings.data}>
          <InlineNotice message={message} tone={toneFor(message)} />
          {Object.values(dirtyState).some(Boolean) ? <InlineNotice message="Unsaved local edits are preserved in the browser until you save them." tone="warning" /> : null}
          <MetricGrid items={[
            { label: 'Runtime Mode', value: modelStrategy.mode || 'lm_studio_local', note: modelStrategy.launch_transport_mix || 'LM Studio local routing' },
            { label: 'Draft Renderer', value: settings.data?.drafting_strategy?.renderer || settings.data?.chatgpt_drafting?.renderer || '-', note: settings.data?.chatgpt_drafting?.enabled ? 'ChatGPT-managed PDF drafting' : 'renderer not configured' },
            { label: 'Runtime Default', value: modelStrategy.model || runtimeModelForm?.model || '-', note: modelStrategy.base_url || runtimeModelForm?.base_url || '-' },
            { label: 'Transport', value: modelStrategy.transport || runtimeModelForm?.transport || '-', note: providerLabel(modelStrategy.provider || runtimeModelForm?.provider) },
            { label: 'API Key', value: 'not required', note: 'LM Studio runs over local HTTP without API-key routing.' },
            { label: 'Profiles', value: formatNumber(advancedProfiles.length), note: 'active router profiles' },
            { label: 'Config', value: settings.data?.readiness?.config_validation?.overall_status || '-', note: 'workspace validation' },
            { label: 'Doctor', value: settings.data?.readiness?.doctor?.overall_status || '-', note: 'runtime + browser readiness' },
            { label: 'Launch', value: settings.data?.readiness?.launch_check?.overall_status || '-', note: 'final release gate' },
          ]} />
          <FindingsList items={readinessFindings.slice(0, 16)} />
        </DataState>
      </Section>

      <Section eyebrow="Drafting" title="ChatGPT Drafting" description="Live resume and cover-letter generation now uses the managed ChatGPT profile. LM Studio stays in scope for screening and application-question answering.">
        <DataState error={settings.error} loading={settings.loading} empty={!chatgptForm}>
          <form className="section-stack" onSubmit={saveChatgptDrafting} onChangeCapture={() => markDirty('chatgpt')}>
            <MetricGrid items={[
              { label: 'Renderer', value: settings.data?.chatgpt_drafting?.renderer || '-', note: 'active document strategy' },
              { label: 'Browser Mode', value: chatgptForm?.browser_mode || 'attached', note: settings.data?.chatgpt_drafting?.browser?.profile_dir_exists ? 'profile directory present' : 'profile directory not initialized yet' },
              { label: 'Parallel Drafts', value: String(chatgptForm?.max_parallel_jobs || 1), note: 'concurrent ChatGPT tabs for document generation' },
              { label: 'Temporary Chat', value: chatgptForm?.use_temporary_chat ? 'enabled' : 'disabled', note: 'disable when ChatGPT attachment downloads are more reliable without it' },
              { label: 'Launch', value: settings.data?.chatgpt_drafting?.launch_status?.last_browser_launch_ok ? 'ok' : 'pending', note: settings.data?.chatgpt_drafting?.launch_status?.last_browser_launch_at || 'no launch recorded' },
              { label: 'Last Draft', value: settings.data?.chatgpt_drafting?.last_result?.success ? 'success' : settings.data?.chatgpt_drafting?.last_error ? 'failed' : 'idle', note: settings.data?.chatgpt_drafting?.last_error || settings.data?.chatgpt_drafting?.last_result?.application_id || 'no draft recorded' },
            ]} />
            <div className="form-grid">
              <label><span>GPT URL</span><input value={chatgptForm?.gpt_url || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, gpt_url: event.target.value })} /></label>
              <label><span>CDP URL</span><input value={chatgptForm?.browser_cdp_url || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, browser_cdp_url: event.target.value })} /></label>
              <label><span>Profile Dir</span><input value={chatgptForm?.profile_dir || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, profile_dir: event.target.value })} /></label>
              <label><span>Downloads Dir</span><input value={chatgptForm?.downloads_dir || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, downloads_dir: event.target.value })} /></label>
              <label><span>Start Marker</span><input value={chatgptForm?.completion_start_marker || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, completion_start_marker: event.target.value })} /></label>
              <label><span>End Marker</span><input value={chatgptForm?.completion_end_marker || ''} onChange={(event) => setChatgptForm({ ...chatgptForm, completion_end_marker: event.target.value })} /></label>
              <label><span>Timeout Seconds</span><input type="number" value={chatgptForm?.timeout_seconds || 240} onChange={(event) => setChatgptForm({ ...chatgptForm, timeout_seconds: Number(event.target.value) })} /></label>
              <label><span>Download Timeout</span><input type="number" value={chatgptForm?.download_timeout_seconds || 120} onChange={(event) => setChatgptForm({ ...chatgptForm, download_timeout_seconds: Number(event.target.value) })} /></label>
              <label><span>Submit Delay (ms)</span><input type="number" value={chatgptForm?.prompt_submit_delay_ms || 300} onChange={(event) => setChatgptForm({ ...chatgptForm, prompt_submit_delay_ms: Number(event.target.value) })} /></label>
              <label><span>Max Parallel Drafts</span><input type="number" value={chatgptForm?.max_parallel_jobs || 10} onChange={(event) => setChatgptForm({ ...chatgptForm, max_parallel_jobs: Number(event.target.value) })} /></label>
              <label><span>Browser Mode</span><input value={chatgptForm?.browser_mode || 'attached'} readOnly /></label>
              <label className="checkbox-field"><input type="checkbox" checked={Boolean(chatgptForm?.enabled)} onChange={(event) => setChatgptForm({ ...chatgptForm, enabled: event.target.checked })} /><span>ChatGPT drafting enabled</span></label>
              <label className="checkbox-field"><input type="checkbox" checked={Boolean(chatgptForm?.launch_if_missing)} onChange={(event) => setChatgptForm({ ...chatgptForm, launch_if_missing: event.target.checked })} /><span>Launch browser if missing</span></label>
              <label className="checkbox-field"><input type="checkbox" checked={Boolean(chatgptForm?.use_temporary_chat)} onChange={(event) => setChatgptForm({ ...chatgptForm, use_temporary_chat: event.target.checked })} /><span>Use ChatGPT temporary chat</span></label>
              <label className="checkbox-field"><input type="checkbox" checked={Boolean(chatgptForm?.make_default)} onChange={(event) => setChatgptForm({ ...chatgptForm, make_default: event.target.checked })} /><span>Keep as default renderer</span></label>
            </div>
            <div className="detail-line">This browser session is separate from ATS submit automation. Downloads are captured into the workspace runtime folder and then normalized into the existing submission artifact names.</div>
            <div className="form-actions">
              <button className="button button-primary" type="submit" disabled={Boolean(savingState.chatgpt)}>{savingState.chatgpt ? 'Saving...' : 'Save ChatGPT Drafting'}</button>
              <button className="button button-ghost" type="button" onClick={launchChatgptBrowser} disabled={Boolean(savingState.chatgptLaunch)}>{savingState.chatgptLaunch ? 'Launching...' : 'Launch Browser'}</button>
              <button className="button button-ghost" type="button" onClick={testChatgptDrafting} disabled={Boolean(savingState.chatgptTest)}>{savingState.chatgptTest ? 'Testing...' : 'Run Draft Test'}</button>
            </div>
          </form>
        </DataState>
      </Section>

      <Section eyebrow="Sources" title="Sources & Automation" description="Manage enabled discovery sources, additive board seeds, and tracked companies without losing unsaved edits to background refreshes.">
        <DataState error={settings.error} loading={settings.loading} empty={!portalForm}>
          <form className="section-stack" onSubmit={savePortals} onChangeCapture={() => markDirty('portals')}>
            <div className="settings-grid">
              {PORTAL_SOURCE_OPTIONS.map((source) => {
                const sourceForm = portalForm?.sources?.[source.id] || blankPortalSource(source.id)
                return (
                  <article key={source.id} className="subpanel settings-card">
                    <div className="eyebrow">{source.label}</div>
                    <div className="activity-meta">
                      {source.launchDefault ? <Badge tone="success">launch default</Badge> : null}
                      {source.experimental ? <Badge tone="warning">experimental</Badge> : null}
                    </div>
                    <div className="detail-line">{source.note}</div>
                    <label className="checkbox-field"><input type="checkbox" checked={Boolean(sourceForm.enabled)} onChange={(event) => setPortalForm((prev) => ({ ...prev, sources: { ...prev.sources, [source.id]: { ...prev.sources[source.id], enabled: event.target.checked } } }))} /><span>{sourceForm.enabled ? 'Enabled for launch scope' : 'Disabled for launch scope'}</span></label>
                    <label><span>Boards</span><textarea rows="5" value={toMultiline(sourceForm.boards)} onChange={(event) => setPortalForm((prev) => ({ ...prev, sources: { ...prev.sources, [source.id]: { ...prev.sources[source.id], boards: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) } } }))} /></label>
                    <label><span>Seed URLs</span><textarea rows="4" value={toMultiline(sourceForm.seed_urls)} onChange={(event) => setPortalForm((prev) => ({ ...prev, sources: { ...prev.sources, [source.id]: { ...prev.sources[source.id], seed_urls: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) } } }))} /></label>
                    <label><span>Seed Domains</span><textarea rows="4" value={toMultiline(sourceForm.seed_domains)} onChange={(event) => setPortalForm((prev) => ({ ...prev, sources: { ...prev.sources, [source.id]: { ...prev.sources[source.id], seed_domains: event.target.value.split('\n').map((item) => item.trim()).filter(Boolean) } } }))} /></label>
                  </article>
                )
              })}
            </div>
            <article className="subpanel settings-card">
              <div className="eyebrow">Tracked Companies</div>
              <div className="detail-line">Optional company-specific biasing inputs. These add priority seeds for discovery, but they no longer cap the broader built-in board universe.</div>
              <div className="tracked-company-list">
                {(portalForm?.tracked_companies || []).map((company, index) => (
                  <div className="tracked-company-card" key={`${company.name || 'company'}-${index}`}>
                    <label><span>Name</span><input value={company.name || ''} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item) }))} /></label>
                    <label><span>Source</span><select value={company.source || 'greenhouse'} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, source: event.target.value } : item) }))}>{PORTAL_SOURCE_OPTIONS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
                    <label><span>Careers URL</span><input value={company.careers_url || ''} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, careers_url: event.target.value } : item) }))} /></label>
                    <label><span>Board</span><input value={company.board || ''} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, board: event.target.value } : item) }))} /></label>
                    <label><span>Notes</span><input value={company.notes || ''} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, notes: event.target.value } : item) }))} /></label>
                    <label className="checkbox-field"><input type="checkbox" checked={Boolean(company.enabled)} onChange={(event) => setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.map((item, itemIndex) => itemIndex === index ? { ...item, enabled: event.target.checked } : item) }))} /><span>Enabled</span></label>
                    <button className="button button-ghost" type="button" onClick={() => { markDirty('portals'); setPortalForm((prev) => ({ ...prev, tracked_companies: prev.tracked_companies.filter((_, itemIndex) => itemIndex !== index) })) }}>Remove</button>
                  </div>
                ))}
              </div>
              <div className="form-actions">
                <button className="button button-ghost" type="button" onClick={() => { markDirty('portals'); setPortalForm((prev) => ({ ...prev, tracked_companies: [...(prev?.tracked_companies || []), blankTrackedCompany()] })) }}>Add Company</button>
                <button className="button button-primary" type="submit" disabled={Boolean(savingState.portals)}>{savingState.portals ? 'Saving...' : 'Save Portals'}</button>
              </div>
            </article>
          </form>
        </DataState>
      </Section>

      <Section eyebrow="Routing" title="Model Routing" description="Set the LM Studio runtime for screening and application-question answering. Role-level overrides stay available below, and legacy writer bindings remain optional rollback paths.">
        <DataState error={settings.error} loading={settings.loading} empty={!runtimeModelForm}>
          <div className="settings-grid">
            <article className="subpanel settings-card">
              <div className="eyebrow">Automation Controls</div>
              <form className="form-grid" onSubmit={saveAutonomous} onChangeCapture={() => markDirty('autonomous')}>
                <label><span>Default Submit Mode</span><select value={autonomousForm?.default_submit_mode || 'auto_submit'} onChange={(event) => setAutonomousForm({ ...autonomousForm, default_submit_mode: event.target.value })}><option value="auto_submit">auto_submit</option><option value="preview_first">preview_first</option></select></label>
                <label><span>Browser Mode</span><select value={autonomousForm?.browser_mode || 'headless'} onChange={(event) => setAutonomousForm({ ...autonomousForm, browser_mode: event.target.value })}><option value="headed">headed (visible browser)</option><option value="headless">headless (background)</option><option value="attached">attached (CDP debug)</option></select></label>
                <label><span>Max Open Tabs</span><input type="number" value={autonomousForm?.max_open_tabs || 0} onChange={(event) => setAutonomousForm({ ...autonomousForm, max_open_tabs: Number(event.target.value) })} /></label>
                <label><span>Ready Queue Threshold</span><input type="number" value={autonomousForm?.ready_to_apply_threshold || 10} onChange={(event) => setAutonomousForm({ ...autonomousForm, ready_to_apply_threshold: Number(event.target.value) })} /></label>
                <label><span>Daily Max Submissions</span><input type="number" value={autonomousForm?.daily_submit_cap || 100} onChange={(event) => setAutonomousForm({ ...autonomousForm, daily_submit_cap: Number(event.target.value) })} /></label>
                <label><span>Per-Company Cap</span><input type="number" value={autonomousForm?.per_company_daily_cap || 0} onChange={(event) => setAutonomousForm({ ...autonomousForm, per_company_daily_cap: Number(event.target.value) })} /></label>
                <label><span>Captcha Strategy</span><select value={autonomousForm?.captcha_strategy || 'skip'} onChange={(event) => setAutonomousForm({ ...autonomousForm, captcha_strategy: event.target.value })}><option value="skip">skip</option><option value="manual">manual</option><option value="solve">solve</option></select></label>
                <label><span>Captcha Provider</span><select value={autonomousForm?.captcha_provider || '2captcha'} onChange={(event) => setAutonomousForm({ ...autonomousForm, captcha_provider: event.target.value })}><option value="2captcha">2captcha</option><option value="capmonster">capmonster</option><option value="anti-captcha">anti-captcha</option></select></label>
                <label><span>Captcha API Key Env</span><input value={autonomousForm?.captcha_api_key_env || 'CAPTCHA_API_KEY'} onChange={(event) => setAutonomousForm({ ...autonomousForm, captcha_api_key_env: event.target.value })} /></label>
                <label><span>Captcha Timeout (s)</span><input type="number" value={autonomousForm?.captcha_solve_timeout_seconds || 300} onChange={(event) => setAutonomousForm({ ...autonomousForm, captcha_solve_timeout_seconds: Number(event.target.value) })} /></label>
                <label><span>Launch Scope</span><input value={Array.isArray(autonomousForm?.production_sources) ? autonomousForm.production_sources.join(', ') : ''} readOnly /></label>
                <div className="detail-line span-all">Autonomous runs stop for the day once recorded submissions reach the daily max. Discovery and queue-building continue normally until that cap is hit.</div>
                <label className="checkbox-field"><input type="checkbox" checked={Boolean(autonomousForm?.enabled)} onChange={(event) => setAutonomousForm({ ...autonomousForm, enabled: event.target.checked })} /><span>Autonomous pipeline enabled</span></label>
                <label className="checkbox-field"><input type="checkbox" checked={Boolean(autonomousForm?.submit_enabled)} onChange={(event) => setAutonomousForm({ ...autonomousForm, submit_enabled: event.target.checked })} /><span>Submit enabled</span></label>
                <label className="checkbox-field"><input type="checkbox" checked={Boolean(autonomousForm?.browser_attach_enabled)} onChange={(event) => setAutonomousForm({ ...autonomousForm, browser_attach_enabled: event.target.checked })} /><span>Browser attach enabled</span></label>
                <label><span>Browser CDP URL</span><input value={autonomousForm?.browser_cdp_url || ''} onChange={(event) => setAutonomousForm({ ...autonomousForm, browser_cdp_url: event.target.value })} /></label>
                <div className="form-actions span-all"><button className="button button-primary" type="submit" disabled={Boolean(savingState.autonomous)}>{savingState.autonomous ? 'Saving...' : 'Save Automation'}</button></div>
              </form>
            </article>

            <article className="subpanel settings-card">
              <div className="eyebrow">Runtime Default</div>
              <form className="form-grid" onSubmit={saveRuntimeModel} onChangeCapture={() => markDirty('runtime')}>
                <label><span>Provider</span><input value="LM Studio" readOnly /></label>
                <label><span>Transport</span><input value="Local HTTP" readOnly /></label>
                <label><span>Model Name</span><input list="runtime-model-catalog" value={runtimeModelForm?.model || ''} onChange={(event) => setRuntimeModelForm({ ...runtimeModelForm, model: event.target.value })} /></label>
                <label><span>Base URL</span><input value={runtimeModelForm?.base_url || ''} placeholder={LMSTUDIO_DEFAULT_HOST} onChange={(event) => setRuntimeModelForm({ ...runtimeModelForm, base_url: event.target.value })} /></label>
                <label><span>Temperature</span><input type="number" step="0.1" value={runtimeModelForm?.temperature || 0} onChange={(event) => setRuntimeModelForm({ ...runtimeModelForm, temperature: Number(event.target.value) })} /></label>
                <label><span>Max Tokens</span><input type="number" value={runtimeModelForm?.max_tokens || 0} onChange={(event) => setRuntimeModelForm({ ...runtimeModelForm, max_tokens: Number(event.target.value) })} /></label>
                <label><span>Preferred Context Window</span><input type="number" value={runtimeModelForm?.preferred_context_window || 0} onChange={(event) => setRuntimeModelForm({ ...runtimeModelForm, preferred_context_window: Number(event.target.value) })} /></label>
                <div className="detail-line span-all">Launch uses LM Studio-local only. Enter the loaded LM Studio model id and the local server base URL.</div>
                <div className="detail-line span-all">{runtimeCatalog.note || `${formatNumber(runtimeCatalog.count || 0)} models available from ${runtimeCatalog.source || 'LM Studio'}.`}</div>
                {runtimeChecks['runtime-model'] ? <div className="detail-line span-all">Last ping: {runtimeChecks['runtime-model'].classification || (runtimeChecks['runtime-model'].ok ? 'ok' : 'failed')} · {formatDate(runtimeChecks['runtime-model'].checked_at)}</div> : null}
                <div className="form-actions span-all">
                  <button className="button button-primary" type="submit" disabled={Boolean(savingState.runtime)}>{savingState.runtime ? 'Saving...' : 'Save Runtime'}</button>
                  <button className="button button-ghost" type="button" onClick={() => loadRuntimeCatalog(true)} disabled={Boolean(savingState.runtimeCatalog)}>{savingState.runtimeCatalog ? 'Refreshing...' : 'Refresh Catalog'}</button>
                  <button className="button button-ghost" type="button" onClick={() => pingModel({ ...runtimeModelForm, name: 'runtime-model' }, 'runtimePing')} disabled={Boolean(savingState.runtimePing)}>{savingState.runtimePing ? 'Pinging...' : 'Ping Runtime'}</button>
                  <button className="button button-ghost" type="button" onClick={installRecommended} disabled={Boolean(savingState.recommended)}>{savingState.recommended ? 'Installing...' : 'Install Recommended Split'}</button>
                  <button className="button button-ghost" type="button" onClick={regenerateDossier} disabled={Boolean(savingState.dossier)}>{savingState.dossier ? 'Regenerating...' : 'Regenerate Dossier'}</button>
                </div>
              </form>
              <datalist id="runtime-model-catalog">
                {(runtimeCatalog.models || []).map((model) => <option key={model.id} value={model.id}>{model.label || model.id}</option>)}
              </datalist>
            </article>
          </div>
        </DataState>
      </Section>

      <Section eyebrow="Model Routing" title="Workflow Families" description="Switch grouped local-model families for screening and question answering. Legacy drafting roles remain available for rollback, but live document drafting now runs through ChatGPT.">
        <DataState error={settings.error} loading={settings.loading} empty={!settings.data}>
          <ModelHotSwap settings={settings.data} onSaved={() => settings.refresh()} onPing={pingModel} loadingState={savingState} />
        </DataState>
      </Section>

      <Section eyebrow="Advanced Routing" title="Role-Level Profiles" description="Fine-grained model control for individual roles, transports, commands, and fallback chains.">
        <DataState error={settings.error} loading={settings.loading} empty={!settings.data}>
          <ProfileInventory profiles={advancedProfiles} checks={runtimeChecks} onPing={pingModel} onDelete={deleteProfile} loadingState={savingState} />
          <form className="form-grid form-compact" onSubmit={saveProfile} onChangeCapture={() => markDirty('profile')}>
            <label><span>Name</span><input value={profileForm.name} onChange={(event) => setProfileForm({ ...profileForm, name: event.target.value })} /></label>
            <label><span>Role</span><input value={profileForm.role} onChange={(event) => setProfileForm({ ...profileForm, role: event.target.value })} /></label>
            <label><span>Provider</span><input value="LM Studio" readOnly /></label>
            <label><span>Transport</span><input value="Local HTTP" readOnly /></label>
            <label><span>Model</span><input value={profileForm.model} onChange={(event) => setProfileForm({ ...profileForm, model: event.target.value })} /></label>
            <label><span>Base URL</span><input value={profileForm.base_url} placeholder={LMSTUDIO_DEFAULT_HOST} onChange={(event) => setProfileForm({ ...profileForm, base_url: event.target.value })} /></label>
            <label><span>Temperature</span><input type="number" step="0.1" value={profileForm.temperature} onChange={(event) => setProfileForm({ ...profileForm, temperature: Number(event.target.value) })} /></label>
            <label><span>Max Tokens</span><input type="number" value={profileForm.max_tokens} onChange={(event) => setProfileForm({ ...profileForm, max_tokens: Number(event.target.value) })} /></label>
            <label><span>Preferred Context Window</span><input type="number" value={profileForm.preferred_context_window} onChange={(event) => setProfileForm({ ...profileForm, preferred_context_window: Number(event.target.value) })} /></label>
            <label><span>Fallback Chain</span><input value={profileForm.fallback_chain_text} onChange={(event) => setProfileForm({ ...profileForm, fallback_chain_text: event.target.value })} placeholder="comma,separated,profile-names" /></label>
            <label><span>Policy Tags</span><input value={profileForm.policy_tags_text} onChange={(event) => setProfileForm({ ...profileForm, policy_tags_text: event.target.value })} placeholder="draft,review,screen" /></label>
            <label className="checkbox-field"><input type="checkbox" checked={Boolean(profileForm.supports_structured_output)} onChange={(event) => setProfileForm({ ...profileForm, supports_structured_output: event.target.checked })} /><span>Structured output</span></label>
            <label className="checkbox-field"><input type="checkbox" checked={Boolean(profileForm.local)} onChange={(event) => setProfileForm({ ...profileForm, local: event.target.checked })} /><span>Mark as local</span></label>
            <div className="form-actions span-all">
              <button className="button button-primary" type="submit" disabled={Boolean(savingState.profile)}>{savingState.profile ? 'Saving...' : 'Save Profile'}</button>
              <button className="button button-ghost" type="button" onClick={() => pingModel({
                name: profileForm.name || 'draft-profile',
                role: profileForm.role,
                provider: LMSTUDIO_DEFAULT_PROVIDER,
                transport: 'local_http',
                model: profileForm.model,
                base_url: profileForm.base_url,
                temperature: Number(profileForm.temperature || 0.2),
                max_tokens: Number(profileForm.max_tokens || 8192),
                preferred_context_window: Number(profileForm.preferred_context_window || 131072),
                local: true,
                command: [],
                working_dir: '',
              }, 'profileDraftPing')} disabled={Boolean(savingState.profileDraftPing)}>{savingState.profileDraftPing ? 'Pinging...' : 'Ping Draft Profile'}</button>
            </div>
          </form>
        </DataState>
      </Section>
    </div>
  )
}
function AutopilotPage({ operator, live }) {
  const auto = usePolledJson('/api/autonomous/status', 6000)
  const questions = usePolledJson('/api/questions/queue', 5000)
  const jobs = usePolledJson('/api/jobs/table?limit=100', 7000)
  const [answers, setAnswers] = useState({})
  const [notice, setNotice] = useState('')
  const [resetting, setResetting] = useState(false)
  const answerKey = (item) => `${item.application_id}::${item.question_id}`
  const questionItems = questions.data?.items || []
  const jobItems = jobs.data?.items || []

  async function startDiscover() { await requestJson('/api/discover', { method: 'POST' }); await Promise.allSettled([live.refresh(), auto.refresh(), jobs.refresh()]) }
  async function startAutonomous() { await requestJson('/api/autonomous/run', { method: 'POST' }); await Promise.allSettled([live.refresh(), auto.refresh(), jobs.refresh(), questions.refresh()]) }

  async function purgeRejected() {
    try {
      const result = await requestJson('/api/jobs/purge-rejected', { method: 'POST' })
      setNotice(`Purged ${result?.purged || 0} rejected jobs.`)
      await Promise.allSettled([jobs.refresh(), auto.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    }
  }

  async function resetOperationalData() {
    setResetting(true)
    try {
      const result = await requestJson('/api/workspace/reset-operational', { method: 'POST' })
      const deleted = result?.deleted || {}
      setNotice(`Reset complete. Cleared ${deleted.applications || 0} applications, ${deleted.jobs || 0} job files, ${deleted.runs || 0} runs.`)
      await Promise.allSettled([auto.refresh(), jobs.refresh(), questions.refresh(), live.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    } finally {
      setResetting(false)
    }
  }

  async function submitAnswer(item) {
    const key = answerKey(item)
    const answer = serializeAnswerDraft(
      item,
      Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(item, item.existing_answer ?? ''),
    )
    if (item.required && !answer) {
      setNotice(`Answer required: ${item.prompt_text}`)
      return
    }
    try {
      await requestJson('/api/questions/answer', { method: 'POST', body: JSON.stringify({ application_id: item.application_id, question_id: item.question_id, answer_text: answer, approve_memory: true, auto_retry: true }) })
      setNotice(`Saved answer for ${item.company} / ${item.title}; future matching prompts will reuse it automatically.`)
      setAnswers((current) => {
        const next = { ...current }
        delete next[key]
        return next
      })
      await Promise.allSettled([questions.refresh(), auto.refresh(), jobs.refresh(), live.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    }
  }

  async function applyFromTable(row) {
    if (!row?.application_id) return
    try {
      await requestJson('/api/review/action', { method: 'POST', body: JSON.stringify({ application_id: row.application_id, action: 'approve' }) })
      setNotice(`Apply action completed for ${row.company} / ${row.role}.`)
      await Promise.allSettled([auto.refresh(), jobs.refresh(), live.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="page-stack">
      <Section eyebrow="Autopilot" title="Queue And Live Activity" description="Long-running work stays visible across navigation and reloads because button state now derives from the backend live run state." actions={<><button className="button button-primary" type="button" onClick={startDiscover} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'discover' ? 'Discovery Running' : 'Discover Jobs'}</button><button className="button button-primary" type="button" onClick={startAutonomous} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'autonomous' ? 'Full Run Running' : 'Full Run'}</button><button className="button button-ghost" type="button" onClick={resetOperationalData} disabled={resetting}>{resetting ? 'Resetting' : 'Reset Operational Data'}</button><button className="button button-ghost" type="button" onClick={purgeRejected}>Purge Rejected</button></>}>
        <DataState error={auto.error} loading={auto.loading} empty={!auto.data}>
          <div className="section-stack">
            <CurrentProcessPanel operator={operator} compact />
            <InlineNotice message={notice} tone={toneFor(notice)} />
            <MetricGrid items={[
              { label: 'Enabled', value: auto.data?.enabled ? 'on' : 'off', note: 'automation switch' },
              { label: 'Submit', value: auto.data?.submit_enabled ? 'on' : 'off', note: 'submission toggle' },
              { label: 'Submit Mode', value: auto.data?.default_submit_mode || '-', note: 'current submit strategy' },
              { label: 'Daily Max', value: `${formatNumber(auto.data?.daily_submitted_today || 0)} / ${formatNumber(auto.data?.daily_submit_cap || 0)}`, note: auto.data?.daily_submit_cap ? `submissions recorded today; ${formatNumber(auto.data?.daily_remaining_capacity || 0)} remaining before the run stops` : 'daily cap not configured' },
              { label: 'Source Mode', value: auto.data?.source_mode === 'greenhouse_launch_mode' ? 'Greenhouse launch mode' : 'Mixed / experimental', note: auto.data?.experimental_sources_enabled?.length ? `experimental on: ${auto.data.experimental_sources_enabled.join(', ')}` : 'experimental sources available but off' },
              { label: 'Drafting Mode', value: auto.data?.drafting_mode === 'serial' ? 'serial' : 'parallel', note: auto.data?.drafting_mode === 'serial' ? 'draft, prepare, and apply one job at a time' : 'multiple drafts can run before apply begins' },
              { label: 'Ready Threshold', value: formatNumber(auto.data?.ready_to_apply_threshold || 10), note: auto.data?.configured_ready_to_apply_threshold && auto.data?.configured_ready_to_apply_threshold !== auto.data?.ready_to_apply_threshold ? `effective threshold ${formatNumber(auto.data?.ready_to_apply_threshold || 10)} from configured ${formatNumber(auto.data?.configured_ready_to_apply_threshold || 10)}` : 'start apply at this depth' },
              { label: 'Queue Depth', value: formatNumber(auto.data?.queue_depth || operator.queue.depth), note: 'active queue' },
              { label: 'Blocked', value: formatNumber(auto.data?.blocked_by_questions || auto.data?.blocked_applications || operator.queue.blocked), note: 'manual answers needed' },
              { label: 'Prompts', value: formatNumber(auto.data?.unresolved_prompts || operator.queue.pendingQuestions), note: 'manual questions' },
              { label: 'Draft Batch', value: auto.data?.drafting_batch?.member_count ? `${formatNumber(auto.data?.drafting_batch?.completed_count || 0)} / ${formatNumber(auto.data?.drafting_batch?.member_count || 0)}` : '-', note: describeDraftBatch(auto.data?.drafting_batch, auto.data?.ready_to_apply_threshold || 10) },
              { label: 'Active Draft Tabs', value: formatNumber(auto.data?.drafting_batch?.active_worker_count || auto.data?.drafting_batch?.active_count || 0), note: `live ChatGPT tabs for the current batch (parallel cap ${formatNumber(auto.data?.drafting_parallel_limit || 0) || 0})` },
              { label: 'Temp Chat', value: operator.temporaryChatStatus || '-', note: operator.temporaryChatCheckedAt || 'no drafting preflight recorded yet' },
            ]} />
          </div>
        </DataState>
      </Section>

      <Section eyebrow="Questions" title="Unresolved Inputs" description="Only the prompts that could not be safely answered are surfaced here.">
        <DataState error={questions.error} loading={questions.loading} empty={!questionItems.length} emptyLabel="No manual questions." emptyDetail="The queue is either ready to apply or waiting on discovery and drafting.">
          <InlineNotice message={notice} tone={toneFor(notice)} />
          <div className="question-list">
            {questionItems.map((item) => {
              const key = answerKey(item)
              const options = questionOptions(item)
              const value = Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(item, item.existing_answer ?? '')
              const isCheckboxGroup = item.widget_type === 'checkbox_group' && options.length > 0
              const isSelect = options.length > 0 && !isCheckboxGroup
              const selectedValues = Array.isArray(value) ? value : []
              return <article className="question-card" key={`${item.application_id}-${item.question_id}`}><div className="activity-meta"><Badge tone={item.has_approved_memory ? 'success' : 'warning'}>{item.question_type}</Badge><span>{item.company}</span><span>{item.title}</span></div><strong>{item.prompt_text}</strong><div className="answer-row">{isCheckboxGroup ? <div className="detail-stack">{options.map((option) => <label key={`${item.question_id}-${option.label}`}><input checked={selectedValues.includes(option.label)} onChange={(event) => setAnswers((current) => { const base = Object.prototype.hasOwnProperty.call(current, key) ? current[key] : selectedValues; const selected = Array.isArray(base) ? [...base] : []; return { ...current, [key]: event.target.checked ? dedupeStrings([...selected, option.label]) : selected.filter((entry) => entry !== option.label) } })} type="checkbox" /> {option.label}</label>)}</div> : isSelect ? <select value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))}><option value="">Select answer</option>{options.map((option) => <option key={`${item.question_id}-${option.value}-${option.label}`} value={option.label}>{option.label}</option>)}</select> : <input value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))} placeholder="Type answer" />}<button className="button button-primary" type="button" onClick={() => submitAnswer(item)}>Save Answer</button></div><div className="cell-meta">Saved answers are reused automatically the next time this prompt appears.</div></article>
            })}
          </div>
        </DataState>
      </Section>

      <LiveTimelineSection operator={operator} live={live} eyebrow="Live Activity" title="Operator Timeline" description="The active run feed is backed by persisted events and trace refs, so blocked submit and silent model failures stop looking like idle UI." />
      <Section eyebrow="Queue" title="Applications Table" description="Operational queue view prioritized around blocker state and actionability."><DataState error={jobs.error} loading={jobs.loading} empty={!jobItems.length} emptyLabel="No jobs in the queue."><JobsTable rows={jobItems} onApply={applyFromTable} /></DataState></Section>
    </div>
  )
}

function ReviewPage({ operator, live }) {
  const review = usePolledJson('/api/review/queue', 7000)
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('application_id') || ''
  const detail = usePolledJson(selectedId ? `/api/applications/${selectedId}` : '/api/review/queue?limit=1', 7000)
  const [notice, setNotice] = useState('')
  const [answers, setAnswers] = useState({})
  const reviewItems = review.data?.items || []

  function selectApplication(applicationId) {
    const nextParams = new URLSearchParams(searchParams)
    if (applicationId) nextParams.set('application_id', applicationId)
    else nextParams.delete('application_id')
    setSearchParams(nextParams, { replace: true })
  }

  useEffect(() => {
    if (!review.data) return
    const queueIds = reviewItems.map((item) => item.application_id)
    if (!queueIds.length) {
      if (selectedId) selectApplication('')
      return
    }
    if (!selectedId || !queueIds.includes(selectedId)) {
      selectApplication(reviewItems[0].application_id)
    }
  }, [reviewItems, searchParams, selectedId, setSearchParams])

  async function takeAction(action) {
    if (!selectedId) return
    try {
      const result = await requestJson('/api/review/action', {
        method: 'POST',
        body: JSON.stringify({ application_id: selectedId, action }),
        timeoutMs: 120_000,
      })
      if (result?.manual_submitted) {
        setNotice('Marked this application as submitted. It now counts toward submitted totals and leaves the review queue.')
      } else if (result?.manual_handoff_opened && action === 'request_input') {
        setNotice('Opened the partially filled application for manual completion. No submit attempt was made.')
      } else if (result?.manual_handoff_opened) {
        setNotice('Submission is still blocked. Opened the partially filled application for manual completion instead of submitting.')
      } else if (result?.blocked) {
        setNotice('Submission is still blocked. The page could not be kept open automatically, so answer the saved blockers here and retry.')
      } else {
        setNotice(`Review action applied: ${action}`)
      }
      const refreshedQueue = await review.refresh()
      const refreshedItems = refreshedQueue?.items || []
      const selectedStillPresent = refreshedItems.some((item) => item.application_id === selectedId)
      if (!selectedStillPresent) {
        selectApplication(refreshedItems[0]?.application_id || '')
      } else {
        await detail.refresh()
      }
      await live.refresh()
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    }
  }

  async function submitReviewAnswer(question) {
    if (!selectedId) return
    const key = `${selectedId}::${question.question_id}`
    const answer = serializeAnswerDraft(
      question,
      Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(question, question.existing_answer ?? ''),
    )
    if (question.required && !answer) {
      setNotice(`Answer required: ${question.prompt_text}`)
      return
    }
    try {
      await requestJson('/api/questions/answer', {
        method: 'POST',
        body: JSON.stringify({
          application_id: selectedId,
          question_id: question.question_id,
          answer_text: answer,
          approve_memory: true,
          auto_retry: false,
        }),
      })
      setNotice(`Saved answer for ${detail.data?.application?.company || 'application'}. Future matching prompts will reuse it automatically.`)
      setAnswers((current) => {
        const next = { ...current }
        delete next[key]
        return next
      })
      await Promise.allSettled([detail.refresh(), review.refresh(), live.refresh()])
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="page-grid review-grid">
      <Section eyebrow="Review Queue" title="Applications Needing Oversight" description="Preview failures, unresolved blockers, and manual approval flow."><CurrentProcessPanel operator={operator} compact /><DataState error={review.error} loading={review.loading} empty={!reviewItems.length} emptyLabel="No review items." emptyDetail="Discovery and drafting must create application records before review can begin."><div className="review-list">{reviewItems.map((item) => <button className={`review-row ${selectedId === item.application_id ? 'selected' : ''}`} key={item.application_id} type="button" onClick={() => selectApplication(item.application_id)}><div><strong>{item.company}</strong><div className="cell-meta">{item.title}</div></div><Badge tone={toneFor(item.review_status || item.status)}>{item.review_status || item.status}</Badge></button>)}</div></DataState></Section>
      <Section eyebrow="Application Detail" title="Selected Application" description="Review status, blockers, questions, and report output."><DataState error={detail.error} loading={detail.loading} empty={!detail.data?.application} emptyLabel="Choose an application." emptyDetail="Select an application from the review queue to inspect details."><InlineNotice message={notice} tone={toneFor(notice)} /><div className="detail-stack"><div className="detail-line"><strong>{detail.data?.application?.company}</strong> / {detail.data?.application?.role}</div><div className="tag-row"><Badge tone={toneFor(detail.data?.application?.status)}>{detail.data?.application?.status || '-'}</Badge><Badge tone={toneFor(detail.data?.submission?.status)}>{detail.data?.submission?.status || 'not_prepared'}</Badge><Badge tone="neutral">{detail.data?.application?.source || '-'}</Badge></div><div className="action-row"><button className="button button-primary" type="button" onClick={() => takeAction('approve')}>Approve / Apply</button><button className="button button-ghost" type="button" onClick={() => takeAction('request_input')}>Open For Manual Input</button><button className="button button-ghost" type="button" onClick={() => takeAction('mark_submitted')}>Mark As Submitted</button><button className="button button-ghost" type="button" onClick={() => takeAction('reject')}>Reject</button></div><div className="cell-meta">Approve / Apply only submits when no blockers remain. Open For Manual Input always opens a partial form for you to finish without submitting. Mark As Submitted records a manual submission you already completed yourself.</div><div className="subpanel"><div className="eyebrow">Blockers</div>{(detail.data?.blockers || []).length ? <div className="detail-stack">{(detail.data?.blockers || []).map((blocker, index) => <div className="detail-line" key={`${blockerLabel(blocker)}-${index}`}>{blockerLabel(blocker)}</div>)}</div> : <div className="detail-line">No blockers recorded.</div>}</div><div className="subpanel"><div className="eyebrow">Questions</div>{(detail.data?.questions || []).length ? <div className="detail-stack">{(detail.data?.questions || []).filter((question) => question.needs_user_input).map((question) => { const key = `${selectedId}::${question.question_id}`; const options = questionOptions(question); const value = Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(question, question.existing_answer ?? ''); const isCheckboxGroup = question.widget_type === 'checkbox_group' && options.length > 0; const isSelect = options.length > 0 && !isCheckboxGroup; const selectedValues = Array.isArray(value) ? value : []; return <div className="question-card" key={question.question_id}><strong>{question.prompt_text}</strong><div className="answer-row">{isCheckboxGroup ? <div className="detail-stack">{options.map((option) => <label key={`${question.question_id}-${option.label}`}><input checked={selectedValues.includes(option.label)} onChange={(event) => setAnswers((current) => { const base = Object.prototype.hasOwnProperty.call(current, key) ? current[key] : selectedValues; const selected = Array.isArray(base) ? [...base] : []; return { ...current, [key]: event.target.checked ? dedupeStrings([...selected, option.label]) : selected.filter((entry) => entry !== option.label) } })} type="checkbox" /> {option.label}</label>)}</div> : isSelect ? <select value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))}><option value="">Select answer</option>{options.map((option) => <option key={`${question.question_id}-${option.value}-${option.label}`} value={option.label}>{option.label}</option>)}</select> : <input value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))} placeholder="Type answer" />}<button className="button button-primary" type="button" onClick={() => submitReviewAnswer(question)}>Save Answer</button></div><div className="cell-meta">Saved answers are reused automatically the next time this prompt appears.</div></div> })}{!(detail.data?.questions || []).some((question) => question.needs_user_input) ? <div className="detail-line">No unresolved questions.</div> : null}{(detail.data?.questions || []).filter((question) => !question.needs_user_input).map((question) => <div className="detail-line" key={question.question_id}>{question.prompt_text}{question.existing_answer ? `: ${question.existing_answer}` : ''}</div>)}</div> : <div className="detail-line">No questions captured.</div>}</div><div className="subpanel"><div className="eyebrow">Report</div><pre className="report-block">{detail.data?.report_markdown || 'No report available.'}</pre></div></div></DataState></Section>
    </div>
  )
}

function RunsPage() {
  const runs = usePolledJson('/api/runs/history', 9000)
  const runItems = runs.data?.items || []
  return <div className="page-stack"><Section eyebrow="Runs" title="Operational History" description="Recent discover, daily, and autonomous runs with compact metrics."><DataState error={runs.error} loading={runs.loading} empty={!runItems.length} emptyLabel="No runs recorded yet."><div className="runs-list">{runItems.map((run) => <article className="finding-card" key={run.run_id}><div className="activity-meta"><Badge tone={toneFor(run.status)}>{run.status}</Badge><span>{run.run_type}</span><span>{formatDate(run.completed_at || run.started_at)}</span></div><strong>{run.run_id}</strong><div className="detail-line">submitted {(run.submitted_count ?? run.submitted_application_ids?.length) || 0} / failed {(run.failed_count ?? run.failed_application_ids?.length) || 0}</div><div className="detail-line">processed {(run.processed_count ?? run.processed_job_ids?.length) || 0} / evaluated {(run.evaluated_count ?? run.evaluated_application_ids?.length) || 0}</div></article>)}</div></DataState></Section></div>
}

function Layout({ operator, children }) {
  const location = useLocation()
  return <div className="app-shell"><header className="topbar"><div><div className="brand-mark">Find My Job</div><h1>Operator Console</h1></div><nav className="topnav">{NAV_ITEMS.map((item) => <Link className={location.pathname === item.to ? 'active' : ''} key={item.to} to={item.to}>{item.label}</Link>)}</nav></header><OperatorRail operator={operator} /><main className="page-shell">{children}</main></div>
}

export function App() {
  const live = useLiveConsole()
  const operator = useMemo(() => deriveOperatorState(live.snapshot, live.connection, live.lastSnapshotAt), [live.connection, live.lastSnapshotAt, live.snapshot])

  return (
    <Layout operator={operator}>
      <Routes>
        <Route path="/" element={<DashboardPage operator={operator} live={live} />} />
        <Route path="/setup" element={<SetupPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/autopilot" element={<AutopilotPage operator={operator} live={live} />} />
        <Route path="/daily" element={<AutopilotPage operator={operator} live={live} />} />
        <Route path="/review" element={<ReviewPage operator={operator} live={live} />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/training" element={<RunsPage />} />
      </Routes>
    </Layout>
  )
}

export { AutopilotPage, DashboardPage, ReviewPage, RunsPage, SettingsPage, SetupPage, requestJson }
