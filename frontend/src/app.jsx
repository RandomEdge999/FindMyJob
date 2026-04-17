
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router-dom'

const NAV_ITEMS = [
  { label: 'Dashboard', to: '/' },
  { label: 'Autopilot', to: '/autopilot' },
  { label: 'Review', to: '/review' },
  { label: 'Settings', to: '/settings' },
]

const NAV_ROUTE_ALIASES = {
  '/daily': '/autopilot',
}

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

function navPathForLocation(pathname) {
  return NAV_ROUTE_ALIASES[pathname] || pathname
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

function ConsoleDisclosure({ title, summary, open, onToggle, children }) {
  return (
    <section className={`console-disclosure ${open ? 'open' : ''}`.trim()}>
      <button aria-expanded={open} className="console-disclosure-toggle" type="button" onClick={onToggle}>
        <div>
          <strong>{title}</strong>
          {summary ? <div className="cell-meta">{summary}</div> : null}
        </div>
        <span className="console-disclosure-state">{open ? 'Hide' : 'Show'}</span>
      </button>
      {open ? <div className="console-disclosure-body">{children}</div> : null}
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
          <tr>
            <th>Company</th>
            <th>Role</th>
            <th>State</th>
            <th>Progress</th>
            <th>Blockers</th>
            <th>Updated</th>
            {onApply ? <th>Action</th> : null}
          </tr>
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
                {onApply ? <td>{row.application_id ? <button className="button button-ghost" type="button" onClick={() => onApply(row)}>Apply</button> : <span className="cell-meta">-</span>}</td> : null}
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
function RunStatusSummaryCard({ operator, title = 'Run Status', description, actions }) {
  const runLabel = operator.isRunning ? operator.runType : 'idle'
  const stageLabel = STAGE_LABELS[operator.stage] || operator.stage || 'Idle'
  const summaryCards = [
    { label: 'Run', value: runLabel, note: operator.latestMessage || 'No active run.' },
    { label: 'Stage', value: stageLabel, note: operator.currentTitle || 'No active target' },
    { label: 'Queue', value: formatNumber(operator.queue.depth), note: `${formatNumber(operator.queue.blocked)} blocked / ${formatNumber(operator.queue.pendingQuestions)} prompts` },
    { label: 'Submitted', value: formatNumber(operator.counters.submitted), note: `${formatNumber(operator.counters.failed)} failed` },
    { label: 'Elapsed', value: operator.elapsed, note: `Last update ${operator.lastSeen}` },
    { label: 'Stream', value: operator.streamHealth, note: operator.modelBadge || operator.modelProfile || '-' },
  ]

  return (
    <article className="panel run-status-card">
      <div className="run-status-head">
        <div>
          <div className="eyebrow">Run Status</div>
          <h3>{title}</h3>
          <p className="section-copy">{description || operator.latestMessage || 'No active run is in progress right now.'}</p>
        </div>
        <div className="run-status-actions">
          <Badge tone={toneForStream(operator.streamHealth)}>{operator.streamHealth}</Badge>
          <Badge tone={toneFor(operator.status)}>{operator.status}</Badge>
          {actions ? <div className="action-row">{actions}</div> : null}
        </div>
      </div>
      <MetricGrid items={summaryCards} className="run-status-metrics" />
      {operator.warningNotice ? <InlineNotice message={operator.warningNotice} tone="danger" /> : null}
      {operator.latestError ? <InlineNotice message={operator.latestError} tone="danger" /> : null}
    </article>
  )
}

function RunsHistoryList({ runItems }) {
  return (
    <div className="runs-list">
      {runItems.map((run) => (
        <article className="finding-card" key={run.run_id}>
          <div className="activity-meta">
            <Badge tone={toneFor(run.status)}>{run.status}</Badge>
            <span>{run.run_type}</span>
            <span>{formatDate(run.completed_at || run.started_at)}</span>
          </div>
          <strong>{run.run_id}</strong>
          <div className="detail-line">submitted {(run.submitted_count ?? run.submitted_application_ids?.length) || 0} / failed {(run.failed_count ?? run.failed_application_ids?.length) || 0}</div>
          <div className="detail-line">processed {(run.processed_count ?? run.processed_job_ids?.length) || 0} / evaluated {(run.evaluated_count ?? run.evaluated_application_ids?.length) || 0}</div>
        </article>
      ))}
    </div>
  )
}

function ReadinessPanel({ readiness, message, resetting, onReset }) {
  const findings = readiness.data?.findings || []
  const profileSurface = readiness.data?.profile_surface || {}
  const profileMode =
    profileSurface.mode === 'local_user_profile'
      ? 'Configured Local Profile'
      : profileSurface.mode === 'advanced_local_overrides'
        ? 'Advanced Local Overrides'
        : 'Sample Mode'
  const activeAdvancedPaths = Array.isArray(profileSurface.active_advanced_paths) ? profileSurface.active_advanced_paths.filter(Boolean) : []

  return (
    <Section eyebrow="Readiness" title="Workspace Readiness" description="Launch checks, profile surface status, and the release gates that determine whether the workspace is ready for real applications." actions={<button className="button button-ghost" type="button" onClick={onReset} disabled={resetting}>{resetting ? 'Resetting...' : 'Reset Operational Data'}</button>}>
      <DataState error={readiness.error} loading={readiness.loading} empty={!readiness.data}>
        <div className="section-stack">
          <MetricGrid items={[
            { label: 'Overall', value: readiness.data?.overall_status || '-', note: 'combined release signal' },
            { label: 'Config', value: readiness.data?.config_validation?.overall_status || '-', note: 'workspace config' },
            { label: 'Doctor', value: readiness.data?.doctor?.overall_status || '-', note: 'runtime and browser readiness' },
            { label: 'Launch', value: readiness.data?.launch_check?.overall_status || '-', note: 'final release gate' },
            { label: 'Sources', value: formatNumber(Object.keys(readiness.data?.sources || {}).length), note: 'configured source families' },
            { label: 'Submit', value: readiness.data?.automation?.submit_enabled ? 'on' : 'off', note: 'submission toggle' },
            { label: 'Profile Mode', value: profileMode, note: profileSurface.configured ? 'local-only candidate data is active' : 'tracked sample data is still active' },
          ]} />
          <div className="subpanel settings-card">
            <div className="eyebrow">Local Profile Surface</div>
            <strong>{profileMode}</strong>
            <p className="section-copy">
              {profileSurface.configured
                ? 'The app is reading local-only candidate data from ignored override paths.'
                : 'The app is still reading tracked sample candidate data. Switch to local-only profile files before real runs.'}
            </p>
            <div className="detail-stack">
              <div className="detail-line"><strong>Local profile file:</strong> <code>{profileSurface.local_path || '.fmj/local-overrides/filefirst/user-profile.yml'}</code></div>
              <div className="detail-line"><strong>Local template:</strong> <code>{profileSurface.local_template_path || '.fmj/local-overrides/filefirst/user-profile.template.yml'}</code></div>
              <div className="detail-line"><strong>Tracked example:</strong> <code>{profileSurface.public_template_path || 'templates/user-profile.local.example.yml'}</code></div>
              {activeAdvancedPaths.length ? <div className="detail-line"><strong>Active advanced overrides:</strong> {activeAdvancedPaths.join(', ')}</div> : null}
            </div>
          </div>
          <InlineNotice message={message} tone={toneFor(message)} />
          {findings.length ? <FindingsList items={findings} /> : null}
        </div>
      </DataState>
    </Section>
  )
}

function DashboardPage({ operator, live }) {
  const dashboard = usePolledJson('/api/dashboard', 7000)
  const runs = usePolledJson('/api/runs/history', 9000)
  const counts = dashboard.data?.snapshot?.counts || {}
  const auto = dashboard.data?.autonomous || {}
  const recentRuns = runs.data?.items || []
  const needsInputCount = operator.counters.blockedByQuestions || auto.blocked_by_questions || auto.blocked_applications || operator.queue.blocked
  const [runHistoryOpen, setRunHistoryOpen] = useState(false)

  const summaryCards = [
    { label: 'Inbox', value: formatNumber(counts.inbox ?? 0), note: 'jobs in workspace' },
    { label: 'Needs Input', value: formatNumber(needsInputCount), note: 'manual answers still needed' },
    { label: 'Ready', value: formatNumber(operator.counters.readyToApply || auto.ready_to_apply || auto.ready_for_submit || 0), note: `${formatNumber(auto.ready_to_apply_threshold || 5)} threshold` },
    { label: 'Submitted', value: formatNumber(operator.counters.submitted), note: 'successful submissions' },
  ]

  useEffect(() => {
    if (recentRuns.length > 0) {
      const mostRecent = recentRuns[0]
      const completedAt = mostRecent?.completed_at || mostRecent?.started_at
      if (completedAt) {
        const hoursAgo = (Date.now() - new Date(completedAt).getTime()) / (1000 * 60 * 60)
        if (hoursAgo < 24) setRunHistoryOpen(true)
      }
    }
  }, [recentRuns.length])

  return (
    <div className="page-stack">
      <Section eyebrow="Dashboard" title="Overview And Health" description="Pipeline health, queue pressure, and discovery coverage at a glance.">
        <DataState error={dashboard.error} loading={dashboard.loading} empty={!dashboard.data}>
          <div className="section-stack">
            <RunStatusSummaryCard
              actions={<><Link className="button button-primary" to="/autopilot">Open Autopilot</Link><Link className="button button-ghost" to="/review">{needsInputCount > 0 ? `Review (${formatNumber(needsInputCount)} need input)` : 'Open Review'}</Link></>}
              description="Use Autopilot to run the pipeline. Use Review only when an application needs your attention."
              operator={operator}
              title={operator.isRunning ? `${String(operator.runType || 'run').replace(/_/g, ' ')} in progress` : 'No Active Run'}
            />
            <MetricGrid items={summaryCards} />
          </div>
        </DataState>
      </Section>

      <SourceHealthPanel operator={operator} />

      <Section eyebrow="Recent Activity" title="Activity & Run History" description="Pipeline events and completed runs. Open Autopilot for the full execution timeline and trace tools." actions={<Link className="button button-ghost" to="/autopilot">Open Full Timeline</Link>}>
        <DataState error={live.error} loading={!operator.events.length && connectionLoading(operator)} empty={!operator.events.length && !recentRuns.length} emptyLabel="No activity yet." emptyDetail="Start discovery or open Autopilot to run the pipeline.">
          <div className="section-stack">
            {operator.events.length ? <ActivityFeed events={operator.eventsDescending.slice(0, 4)} /> : null}
            <ConsoleDisclosure
              open={runHistoryOpen}
              onToggle={() => setRunHistoryOpen((c) => !c)}
              summary={`${formatNumber(recentRuns.length)} recent run(s) recorded.`}
              title="Run History"
            >
              <DataState error={runs.error} loading={runs.loading} empty={!recentRuns.length} emptyLabel="No runs recorded yet.">
                <RunsHistoryList runItems={recentRuns.slice(0, 5)} />
              </DataState>
            </ConsoleDisclosure>
          </div>
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
  const readiness = usePolledJson('/api/setup/readiness', 8000)
  const [message, setMessage] = useState('')
  const [readinessMessage, setReadinessMessage] = useState('')
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
  const [resetting, setResetting] = useState(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const openSection = searchParams.get('section') || ''

  function toggleSection(key) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (next.get('section') === key) { next.delete('section') } else { next.set('section', key) }
      return next
    }, { replace: true })
  }

  function isSectionOpen(key) {
    return openSection === key
  }

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

  useEffect(() => {
    if (!openSection && readiness.data?.overall_status && readiness.data.overall_status !== 'pass') {
      toggleSection('readiness')
    }
  }, [readiness.data?.overall_status])

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

  async function resetOperationalData() {
    setResetting(true)
    try {
      const result = await requestJson('/api/workspace/reset-operational', { method: 'POST' })
      const deleted = result?.deleted || {}
      setReadinessMessage(`Reset complete. Cleared ${deleted.applications || 0} applications, ${deleted.submissions || 0} submissions, ${deleted.runs || 0} runs.`)
      await Promise.allSettled([readiness.refresh(), settings.refresh()])
    } catch (err) {
      setReadinessMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="page-stack">
      <Section eyebrow="Settings" title="Configuration" description="Expand any section to configure. Sections auto-open when they need attention.">
        <DataState error={settings.error} loading={settings.loading} empty={!settings.data}>
          <div className="section-stack">
            <InlineNotice message={message} tone={toneFor(message)} />
            {Object.values(dirtyState).some(Boolean) ? <InlineNotice message="Unsaved local edits stay in the browser until you save them." tone="warning" /> : null}
            <MetricGrid items={[
              { label: 'Launch', value: settings.data?.readiness?.launch_check?.overall_status || '-', note: 'final release gate' },
              { label: 'Draft Renderer', value: settings.data?.drafting_strategy?.renderer || settings.data?.chatgpt_drafting?.renderer || '-', note: settings.data?.chatgpt_drafting?.enabled ? 'ChatGPT-managed' : 'not configured' },
              { label: 'Runtime', value: modelStrategy.model || runtimeModelForm?.model || '-', note: providerLabel(modelStrategy.provider || runtimeModelForm?.provider) },
              { label: 'Profiles', value: formatNumber(advancedProfiles.length), note: 'active router profiles' },
            ]} />
          </div>
        </DataState>
      </Section>

      <ConsoleDisclosure
        open={isSectionOpen('readiness')}
        onToggle={() => toggleSection('readiness')}
        summary={`Overall: ${readiness.data?.overall_status || 'loading'} · Config: ${readiness.data?.config_validation?.overall_status || '-'} · Doctor: ${readiness.data?.doctor?.overall_status || '-'} · Launch: ${readiness.data?.launch_check?.overall_status || '-'}`}
        title="Readiness & Workspace Health"
      >
        <ReadinessPanel message={readinessMessage} onReset={resetOperationalData} readiness={readiness} resetting={resetting} />
      </ConsoleDisclosure>

      <ConsoleDisclosure
        open={isSectionOpen('chatgpt')}
        onToggle={() => toggleSection('chatgpt')}
        summary={`${settings.data?.chatgpt_drafting?.enabled ? 'Enabled' : 'Disabled'} · ${chatgptForm?.browser_mode || 'attached'} · ${String(chatgptForm?.max_parallel_jobs || 1)} parallel · Last: ${settings.data?.chatgpt_drafting?.last_result?.success ? 'success' : settings.data?.chatgpt_drafting?.last_error ? 'failed' : 'idle'}`}
        title="ChatGPT Drafting"
      >
        <DataState error={settings.error} loading={settings.loading} empty={!chatgptForm}>
          <form className="section-stack" onSubmit={saveChatgptDrafting} onChangeCapture={() => markDirty('chatgpt')}>
            <MetricGrid items={[
              { label: 'Renderer', value: settings.data?.chatgpt_drafting?.renderer || '-', note: 'active document strategy' },
              { label: 'Browser Mode', value: chatgptForm?.browser_mode || 'attached', note: settings.data?.chatgpt_drafting?.browser?.profile_dir_exists ? 'profile directory present' : 'profile directory not initialized yet' },
              { label: 'Parallel Drafts', value: String(chatgptForm?.max_parallel_jobs || 1), note: 'concurrent ChatGPT tabs for document generation' },
              { label: 'Temporary Chat', value: chatgptForm?.use_temporary_chat ? 'enabled' : 'disabled', note: 'disable when downloads are more reliable without it' },
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
            <div className="detail-line">This browser session is separate from ATS submit automation. Downloads land in the runtime folder and are then normalized into the usual submission artifact names.</div>
            <div className="form-actions">
              <button className="button button-primary" type="submit" disabled={Boolean(savingState.chatgpt)}>{savingState.chatgpt ? 'Saving...' : 'Save ChatGPT Drafting'}</button>
              <button className="button button-ghost" type="button" onClick={launchChatgptBrowser} disabled={Boolean(savingState.chatgptLaunch)}>{savingState.chatgptLaunch ? 'Launching...' : 'Launch Browser'}</button>
              <button className="button button-ghost" type="button" onClick={testChatgptDrafting} disabled={Boolean(savingState.chatgptTest)}>{savingState.chatgptTest ? 'Testing...' : 'Run Draft Test'}</button>
            </div>
          </form>
        </DataState>
      </ConsoleDisclosure>

      <ConsoleDisclosure
        open={isSectionOpen('sources')}
        onToggle={() => toggleSection('sources')}
        summary={PORTAL_SOURCE_OPTIONS.map((s) => `${s.label}: ${portalForm?.sources?.[s.id]?.enabled ? 'on' : 'off'}`).join(' · ') + ` · ${(portalForm?.tracked_companies || []).length} tracked companies`}
        title="Sources & Discovery"
      >
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
              <div className="detail-line">Optional company-specific inputs. These add priority seeds for discovery without narrowing the broader board universe.</div>
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
      </ConsoleDisclosure>

      <ConsoleDisclosure
        open={isSectionOpen('automation')}
        onToggle={() => toggleSection('automation')}
        summary={`Pipeline: ${autonomousForm?.enabled ? 'on' : 'off'} · Submit: ${autonomousForm?.submit_enabled ? 'on' : 'off'} · Model: ${runtimeModelForm?.model || '-'} · ${providerLabel(runtimeModelForm?.provider)}`}
        title="Automation & Runtime"
      >
        <DataState error={settings.error} loading={settings.loading} empty={!runtimeModelForm}>
          <div className="settings-grid">
            <article className="subpanel settings-card">
              <div className="eyebrow">Automation Defaults</div>
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
                <div className="detail-line span-all">Autonomous submission stops for the day once recorded submissions reach the daily max. Discovery and queue-building continue until that cap is hit.</div>
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
                <div className="detail-line span-all">Launch uses LM Studio-local only. Enter the loaded model id and the local server base URL.</div>
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
      </ConsoleDisclosure>

      <ConsoleDisclosure
        open={isSectionOpen('models')}
        onToggle={() => toggleSection('models')}
        summary={`${formatNumber(advancedProfiles.length)} profiles · ${modelStrategy.mode || 'lm_studio_local'} routing`}
        title="Models & Profiles"
      >
        <DataState error={settings.error} loading={settings.loading} empty={!settings.data}>
          <div className="section-stack">
            <div className="subpanel settings-card">
              <div className="eyebrow">Workflow Families</div>
              <div className="detail-line">Switch grouped local-model families for screening and question answering. Legacy drafting roles remain available for rollback, but live document drafting runs through ChatGPT.</div>
              <ModelHotSwap settings={settings.data} onSaved={() => settings.refresh()} onPing={pingModel} loadingState={savingState} />
            </div>

            <div className="subpanel settings-card">
              <div className="eyebrow">Role-Level Profiles</div>
              <div className="detail-line">Fine-grained control for individual roles, transports, commands, and fallback chains.</div>
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
            </div>
          </div>
        </DataState>
      </ConsoleDisclosure>
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

  const unresolvedCount = questionItems.filter((item) => !item.has_approved_memory).length
  const [timelineOpen, setTimelineOpen] = useState(false)

  return (
    <div className="page-stack">
      <Section eyebrow="Autopilot" title="Execution Workspace" description="Run controls, queue metrics, and unresolved inputs on the left. Application table and live timeline on the right.">
        <DataState error={auto.error} loading={auto.loading} empty={!auto.data}>
          <RunStatusSummaryCard
            actions={<><button className="button button-primary" type="button" onClick={startDiscover} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'discover' ? 'Discovery Running' : 'Discover Jobs'}</button><button className="button button-primary" type="button" onClick={startAutonomous} disabled={operator.isRunning}>{operator.isRunning && operator.runType === 'autonomous' ? 'Full Run Running' : 'Full Run'}</button><button className="button button-ghost" type="button" onClick={resetOperationalData} disabled={resetting}>{resetting ? 'Resetting' : 'Reset Operational Data'}</button><button className="button button-ghost" type="button" onClick={purgeRejected}>Purge Rejected</button></>}
            description="Start discovery, run the full pipeline, and monitor queue movement here."
            operator={operator}
            title={operator.isRunning ? `${String(operator.runType || 'run').replace(/_/g, ' ')} in progress` : 'Ready to run'}
          />
          <InlineNotice message={notice} tone={toneFor(notice)} />
        </DataState>
      </Section>

      <div className="autopilot-grid">
        <div className="autopilot-grid-left">
          <DataState error={auto.error} loading={auto.loading} empty={!auto.data}>
            <MetricGrid items={[
              { label: 'Daily Max', value: `${formatNumber(auto.data?.daily_submitted_today || 0)} / ${formatNumber(auto.data?.daily_submit_cap || 0)}`, note: `${formatNumber(auto.data?.daily_remaining_capacity || 0)} remaining` },
              { label: 'Queue Depth', value: formatNumber(auto.data?.queue_depth || operator.queue.depth), note: 'active queue' },
              { label: 'Blocked', value: formatNumber(auto.data?.blocked_by_questions || auto.data?.blocked_applications || operator.queue.blocked), note: 'answers needed' },
              { label: 'Prompts', value: formatNumber(auto.data?.unresolved_prompts || operator.queue.pendingQuestions), note: 'manual questions' },
              { label: 'Draft Batch', value: auto.data?.drafting_batch?.member_count ? `${formatNumber(auto.data?.drafting_batch?.completed_count || 0)} / ${formatNumber(auto.data?.drafting_batch?.member_count || 0)}` : '-', note: describeDraftBatch(auto.data?.drafting_batch, auto.data?.ready_to_apply_threshold || 10) },
              { label: 'Drafting Mode', value: auto.data?.drafting_mode === 'serial' ? 'serial' : 'parallel', note: auto.data?.drafting_mode === 'serial' ? 'one at a time' : 'concurrent' },
            ]} />
          </DataState>

          <ConsoleDisclosure
            open={unresolvedCount > 0}
            onToggle={() => {}}
            summary={`${formatNumber(questionItems.length)} total · ${formatNumber(unresolvedCount)} unresolved`}
            title="Question Queue"
          >
            <DataState error={questions.error} loading={questions.loading} empty={!questionItems.length} emptyLabel="No manual questions." emptyDetail="The queue is either ready to apply or waiting on discovery and drafting.">
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
          </ConsoleDisclosure>
        </div>

        <div className="autopilot-grid-right">
          <DataState error={jobs.error} loading={jobs.loading} empty={!jobItems.length} emptyLabel="No jobs in the queue.">
            <JobsTable rows={jobItems} onApply={applyFromTable} />
          </DataState>

          <ConsoleDisclosure
            open={timelineOpen || operator.isRunning}
            onToggle={() => setTimelineOpen((c) => !c)}
            summary={operator.isRunning ? 'Active run in progress' : 'Last run events'}
            title="Live Timeline"
          >
            <LiveTimelineSection operator={operator} live={live} eyebrow="Live Activity" title="Operator Timeline" description="The active run feed is backed by persisted events and trace refs." />
          </ConsoleDisclosure>
        </div>
      </div>
    </div>
  )
}

const REVIEW_TABS = [
  { key: 'summary', label: 'Summary' },
  { key: 'questions', label: 'Questions' },
  { key: 'handoff', label: 'Manual Handoff' },
  { key: 'artifacts', label: 'Artifacts' },
  { key: 'history', label: 'History' },
]

const REVIEW_SECTION_FROM_TAB = {
  summary: 'needs_attention',
  questions: 'questions',
  handoff: 'handoff',
  artifacts: 'documents',
  history: 'advanced',
}

const REVIEW_TAB_FROM_SECTION = {
  needs_attention: 'summary',
  questions: 'questions',
  handoff: 'handoff',
  documents: 'artifacts',
  advanced: 'history',
}

const REVIEW_QUEUE_FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'needs_input', label: 'Needs Input' },
  { key: 'manual_handoff', label: 'Manual Handoff' },
  { key: 'ready', label: 'Ready' },
]

const REVIEW_ACTION_LABELS = {
  approve: 'Approve / Apply',
  request_input: 'Open For Manual Input',
  sync_manual_input: 'Sync Browser Changes',
  mark_submitted: 'Mark As Submitted',
  reject: 'Reject',
  save_answers: 'Save Answers',
  review_summary: 'Review Summary',
}

const REVIEW_SEVERITY_WEIGHT = { danger: 3, warning: 2, success: 1, neutral: 0 }

function reviewActionLabel(action) {
  return REVIEW_ACTION_LABELS[action] || String(action || 'Review').replace(/_/g, ' ')
}

function reviewActionTone(action) {
  if (action === 'approve' || action === 'mark_submitted') return 'success'
  if (action === 'reject') return 'danger'
  if (action === 'sync_manual_input' || action === 'request_input' || action === 'save_answers') return 'warning'
  return 'neutral'
}

function reviewSectionFromTab(tab) {
  return REVIEW_SECTION_FROM_TAB[String(tab || '').toLowerCase()] || 'needs_attention'
}

function reviewTabForSection(section) {
  return REVIEW_TAB_FROM_SECTION[section] || 'summary'
}

function reviewSeverityWeight(summary) {
  return REVIEW_SEVERITY_WEIGHT[String(summary?.severity || 'neutral')] ?? 0
}

function reviewSortComparator(sortState) {
  const key = sortState?.key || 'default'
  const direction = sortState?.direction === 'asc' ? 1 : -1
  const compareString = (left, right) => String(left || '').localeCompare(String(right || ''), undefined, { sensitivity: 'base' })
  const compareNumber = (left, right) => safeNumber(left) - safeNumber(right)
  return (left, right) => {
    if (key === 'default') {
      const severityDelta = reviewSeverityWeight(right.review_summary) - reviewSeverityWeight(left.review_summary)
      if (severityDelta) return severityDelta
      const blockerDelta = safeNumber(right.review_summary?.blocker_count) - safeNumber(left.review_summary?.blocker_count)
      if (blockerDelta) return blockerDelta
      return compareString(left.company, right.company)
    }
    let base = 0
    switch (key) {
      case 'company':
        base = compareString(left.company, right.company)
        break
      case 'status':
        base = compareString(left.review_status || left.status, right.review_status || right.status)
        break
      case 'blockers':
        base = compareNumber(left.review_summary?.blocker_count, right.review_summary?.blocker_count)
        break
      case 'ats':
        base = compareString(left.classification?.ats_family || left.source, right.classification?.ats_family || right.source)
        break
      case 'handoff':
        base = compareNumber(left.manual_handoff?.active ? 1 : 0, right.manual_handoff?.active ? 1 : 0) ||
          compareNumber(left.manual_handoff?.pending_count, right.manual_handoff?.pending_count)
        break
      case 'next_action':
        base = compareString(left.review_summary?.next_action, right.review_summary?.next_action)
        break
      default:
        base = compareString(left.company, right.company)
        break
    }
    if (base !== 0) return base * direction
    return compareString(left.company, right.company)
  }
}

function queueMatchesSearch(item, search) {
  const text = String(search || '').trim().toLowerCase()
  if (!text) return true
  const haystack = [
    item.company,
    item.title,
    item.source,
    item.review_status,
    item.status,
    item.classification?.ats_family,
    item.classification?.board_family,
    item.review_summary?.next_action,
    ...(item.review_summary?.blocker_labels || []),
    ...(item.review_summary?.warning_labels || []),
    ...(item.remaining_blockers || []).map((blocker) => blocker?.label || blocker?.category),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(text)
}

function isEditableShortcutTarget(target) {
  if (!target || !(target instanceof HTMLElement)) return false
  const tag = String(target.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button' || Boolean(target.isContentEditable)
}

function reviewQueueMatchesFilter(item, filterKey) {
  const summary = item.review_summary || {}
  const handoff = item.manual_handoff || {}
  const unresolvedCount = safeNumber(summary.unresolved_question_count)
  const blockerCount = safeNumber(summary.blocker_count)
  const readyForSubmit = Boolean(summary.ready_for_submit) || (blockerCount === 0 && unresolvedCount === 0 && !handoff.active)
  if (filterKey === 'needs_input') return blockerCount > 0 || unresolvedCount > 0
  if (filterKey === 'manual_handoff') return Boolean(handoff.active)
  if (filterKey === 'ready') return readyForSubmit && !handoff.active
  return true
}

function reviewQueueAttentionSummary(item) {
  const summary = item.review_summary || {}
  const handoff = item.manual_handoff || {}
  const blockerCount = safeNumber(summary.blocker_count)
  const warningCount = safeNumber(summary.warning_count)
  const unresolvedCount = safeNumber(summary.unresolved_question_count)
  if (blockerCount > 0) return `${formatNumber(blockerCount)} blocker${blockerCount === 1 ? '' : 's'} to clear`
  if (unresolvedCount > 0) return `${formatNumber(unresolvedCount)} answer${unresolvedCount === 1 ? '' : 's'} still needed`
  if (handoff.active) return 'Manual handoff is open'
  if (warningCount > 0) return `${formatNumber(warningCount)} warning${warningCount === 1 ? '' : 's'} to review`
  return 'Ready to move forward'
}

function reviewQueueSupportingMeta(item) {
  return dedupeStrings([
    item.classification?.ats_family || item.classification?.board_family || '',
    item.source || '',
  ]).join(' / ')
}

function reviewSourceMeta(detail) {
  return dedupeStrings([
    detail?.application?.source || '',
    detail?.summary?.classification?.ats_family || detail?.job?.ats_family || '',
  ]).join(' / ')
}

function reviewArtifactForKinds(artifacts, ...kinds) {
  return (artifacts || []).find((artifact) => kinds.includes(artifact.kind)) || null
}

function ReviewQueueTable({ items, selectedId, onSelect, onSort, sortState }) {
  return (
    <div className="table-wrap review-table-wrap">
      <table className="data-table review-data-table">
        <thead>
          <tr>
            <th><button className="review-sort" type="button" onClick={() => onSort('company')}>Company / Role</button></th>
            <th><button className="review-sort" type="button" onClick={() => onSort('status')}>Status</button></th>
            <th><button className="review-sort" type="button" onClick={() => onSort('blockers')}>Blockers</button></th>
            <th><button className="review-sort" type="button" onClick={() => onSort('ats')}>ATS / Source</button></th>
            <th><button className="review-sort" type="button" onClick={() => onSort('handoff')}>Handoff</button></th>
            <th><button className="review-sort" type="button" onClick={() => onSort('next_action')}>Next Action</button></th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const summary = item.review_summary || {}
            const handoff = item.manual_handoff || {}
            const blockerSummary = summary.blocker_count
              ? `${formatNumber(summary.blocker_count)} blocker${summary.blocker_count === 1 ? '' : 's'}`
              : summary.warning_count
                ? `${formatNumber(summary.warning_count)} warning${summary.warning_count === 1 ? '' : 's'}`
                : 'Clear'
            return (
              <tr
                className={`review-row-table ${selectedId === item.application_id ? 'selected' : ''}`.trim()}
                key={item.application_id}
                onClick={() => onSelect(item.application_id)}
              >
                <td>
                  <div className="review-cell-stack">
                    <strong>{item.company}</strong>
                    <div className="cell-meta">{item.title}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={toneFor(summary.severity || item.review_status || item.status)}>{item.review_status || item.status}</Badge>
                    <div className="cell-meta">{item.status || '-'}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={summary.blocker_count ? 'danger' : summary.warning_count ? 'warning' : 'success'}>{blockerSummary}</Badge>
                    <div className="cell-meta">{summary.unresolved_question_count ? `${formatNumber(summary.unresolved_question_count)} unresolved prompt${summary.unresolved_question_count === 1 ? '' : 's'}` : 'No unresolved prompts'}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <div>{item.classification?.ats_family || item.classification?.board_family || '-'}</div>
                    <div className="cell-meta">{item.source || '-'}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={handoff.active ? 'warning' : 'neutral'}>{handoff.status || 'idle'}</Badge>
                    <div className="cell-meta">{handoff.active ? `pending ${formatNumber(handoff.pending_count || 0)}` : 'not active'}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={reviewActionTone(summary.next_action)}>{reviewActionLabel(summary.next_action)}</Badge>
                    <div className="cell-meta">{summary.next_action_reason || '-'}</div>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="cell-meta review-table-note">Sorted by {sortState.key === 'default' ? 'severity, blocker depth, company' : sortState.key.replace(/_/g, ' ')}.</div>
    </div>
  )
}

function ReviewTabNav({ activeTab, onChange }) {
  return (
    <div className="review-tab-nav" role="tablist" aria-label="Review detail tabs">
      {REVIEW_TABS.map((tab) => (
        <button
          aria-selected={activeTab === tab.key}
          className={`review-tab ${activeTab === tab.key ? 'active' : ''}`.trim()}
          key={tab.key}
          role="tab"
          type="button"
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}

function ReviewQuestionsTab({ questions, selectedId, answers, setAnswers, onSave }) {
  const unresolved = (questions || []).filter((question) => question.needs_user_input)
  const resolved = (questions || []).filter((question) => !question.needs_user_input)
  return (
    <div className="detail-stack">
      <div className="subpanel">
        <div className="eyebrow">Unresolved Questions</div>
        {unresolved.length ? (
          <div className="detail-stack">
            {unresolved.map((question) => {
              const key = `${selectedId}::${question.question_id}`
              const options = questionOptions(question)
              const value = Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(question, question.existing_answer ?? '')
              const isCheckboxGroup = question.widget_type === 'checkbox_group' && options.length > 0
              const isSelect = options.length > 0 && !isCheckboxGroup
              const selectedValues = Array.isArray(value) ? value : []
              return (
                <div className="question-card" key={question.question_id}>
                  <div className="activity-meta">
                    <Badge tone={question.required ? 'warning' : 'neutral'}>{question.required ? 'required' : 'optional'}</Badge>
                    <span>{question.question_type || question.widget_type || 'question'}</span>
                    <span>{question.verification_status || 'needs_user_input'}</span>
                  </div>
                  <strong>{question.prompt_text}</strong>
                  <div className="answer-row">
                    {isCheckboxGroup ? (
                      <div className="detail-stack checkbox-stack">
                        {options.map((option) => (
                          <label key={`${question.question_id}-${option.label}`}>
                            <input
                              checked={selectedValues.includes(option.label)}
                              onChange={(event) => setAnswers((current) => {
                                const base = Object.prototype.hasOwnProperty.call(current, key) ? current[key] : selectedValues
                                const selected = Array.isArray(base) ? [...base] : []
                                return {
                                  ...current,
                                  [key]: event.target.checked
                                    ? dedupeStrings([...selected, option.label])
                                    : selected.filter((entry) => entry !== option.label),
                                }
                              })}
                              type="checkbox"
                            /> {option.label}
                          </label>
                        ))}
                      </div>
                    ) : isSelect ? (
                      <select value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))}>
                        <option value="">Select answer</option>
                        {options.map((option) => <option key={`${question.question_id}-${option.value}-${option.label}`} value={option.label}>{option.label}</option>)}
                      </select>
                    ) : (
                      <input value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))} placeholder="Type answer" />
                    )}
                    <button className="button button-primary" type="button" onClick={() => onSave(question)}>Save Answer</button>
                  </div>
                  <div className="cell-meta">Saved answers are reused automatically the next time this prompt appears.</div>
                </div>
              )
            })}
          </div>
        ) : <div className="detail-line">No unresolved questions.</div>}
      </div>
      <div className="subpanel">
        <div className="eyebrow">Resolved Answers</div>
        {resolved.length ? (
          <div className="detail-stack">
            {resolved.map((question) => (
              <div className="review-resolved-answer" key={question.question_id}>
                <div className="detail-line"><strong>{question.prompt_text}</strong></div>
                <div className="cell-meta">{question.existing_answer || 'Saved with no visible answer text.'}</div>
              </div>
            ))}
          </div>
        ) : <div className="detail-line">No resolved answers yet.</div>}
      </div>
    </div>
  )
}

function ReviewSummaryTab({ detail }) {
  const application = detail?.application || {}
  const summary = detail?.summary || {}
  const blockers = detail?.blockers || []
  const reportMarkdown = detail?.report_markdown || ''
  return (
    <div className="detail-stack">
      <MetricGrid items={[
        { label: 'Score', value: application.score ?? '-', note: 'evaluation score' },
        { label: 'Grade', value: application.grade || '-', note: 'evaluation grade' },
        { label: 'Missing', value: formatNumber(summary.missing_required_count || 0), note: 'required fields still missing' },
        { label: 'Ungrounded', value: formatNumber(summary.ungrounded_count || 0), note: 'answers without grounding' },
        { label: 'Low Confidence', value: formatNumber(summary.low_confidence_count || 0), note: 'answers needing review' },
        { label: 'Warnings', value: formatNumber(summary.warning_count || 0), note: 'non-blocking warnings' },
      ]} />
      <div className="subpanel">
        <div className="eyebrow">Recommended Next Action</div>
        <div className="review-next-action">
          <Badge tone={reviewActionTone(summary.next_action)}>{reviewActionLabel(summary.next_action)}</Badge>
          <div className="detail-line">{summary.next_action_reason || 'No recommendation recorded.'}</div>
        </div>
      </div>
      <div className="review-summary-grid">
        <div className="subpanel">
          <div className="eyebrow">Blockers</div>
          {blockers.length ? (
            <div className="detail-stack">
              {blockers.map((blocker, index) => (
                <div className="review-chip-row" key={`${blockerLabel(blocker)}-${index}`}>
                  <Badge tone={blocker?.category === 'warning' ? 'warning' : 'danger'}>{blocker?.category || 'blocker'}</Badge>
                  <div className="cell-meta">{blockerLabel(blocker)}</div>
                </div>
              ))}
            </div>
          ) : <div className="detail-line">No blockers recorded.</div>}
        </div>
        <div className="subpanel">
          <div className="eyebrow">Screening And Classification</div>
          <div className="detail-stack">
            <div className="detail-line">Screening: {summary.screening_status || detail?.job?.screening_status || '-'}</div>
            <div className="detail-line">ATS family: {summary.classification?.ats_family || detail?.job?.ats_family || '-'}</div>
            <div className="detail-line">Board family: {summary.classification?.board_family || detail?.job?.board_family || '-'}</div>
            <div className="detail-line">Automation tier: {summary.classification?.automation_tier || detail?.job?.automation_tier || '-'}</div>
            <div className="detail-line">Login wall detected: {summary.login_wall_detected ? 'yes' : 'no'}</div>
            {summary.hard_reject_reason ? <div className="detail-line">Hard reject reason: {summary.hard_reject_reason}</div> : null}
            {summary.auth_reject_reason ? <div className="detail-line">Auth reject reason: {summary.auth_reject_reason}</div> : null}
          </div>
        </div>
      </div>
      <div className="subpanel">
        <div className="eyebrow">Report Output</div>
        <pre className="report-block">{reportMarkdown || 'No report available.'}</pre>
      </div>
    </div>
  )
}

function ReviewHandoffTab({ manualHandoffWatch }) {
  const recentAnswers = manualHandoffWatch?.recent_answers || []
  return (
    <div className="detail-stack">
      <MetricGrid items={[
        { label: 'Status', value: manualHandoffWatch?.status || (manualHandoffWatch?.active ? 'watching' : 'idle'), note: 'current watch state' },
        { label: 'Last Sync', value: manualHandoffWatch?.last_synced_at ? formatDate(manualHandoffWatch.last_synced_at) : '-', note: 'latest sync from parked page' },
        { label: 'Saved Answers', value: formatNumber(manualHandoffWatch?.synced_question_count || 0), note: 'all captured answers' },
        { label: 'Blanks Filled', value: formatNumber(manualHandoffWatch?.filled_blank_count || 0), note: 'previously blank fields' },
        { label: 'Corrections', value: formatNumber(manualHandoffWatch?.corrected_answer_count || 0), note: 'existing answers corrected' },
        { label: 'Pending Text', value: formatNumber(manualHandoffWatch?.pending_count || 0), note: 'debounced text inputs waiting to settle' },
      ]} />
      <div className="subpanel">
        <div className="eyebrow">Tracked Browser Page</div>
        {manualHandoffWatch?.last_page_url ? (
          <a href={manualHandoffWatch.last_page_url} rel="noreferrer" target="_blank">{manualHandoffWatch.last_page_url}</a>
        ) : <div className="detail-line">No parked browser page URL recorded yet.</div>}
      </div>
      <div className="subpanel">
        <div className="eyebrow">Recent Learned Changes</div>
        {recentAnswers.length ? (
          <div className="table-wrap">
            <table className="data-table review-data-table handoff-change-table">
              <thead>
                <tr>
                  <th>Question</th>
                  <th>Previous</th>
                  <th>New</th>
                  <th>Change Type</th>
                </tr>
              </thead>
              <tbody>
                {recentAnswers.map((item, index) => (
                  <tr key={`${item.question_id || item.prompt_text}-${index}`}>
                    <td>{item.prompt_text || item.question_id || '-'}</td>
                    <td>{item.previous_answer || '-'}</td>
                    <td>{item.answer_text || '-'}</td>
                    <td><Badge tone={item.filled_blank ? 'success' : 'warning'}>{item.change_type || (item.filled_blank ? 'filled_blank' : 'corrected_answer')}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="detail-line">No browser-synced answers recorded yet.</div>}
      </div>
    </div>
  )
}

function ReviewArtifactsTab({ artifacts }) {
  const groups = {
    primary: (artifacts || []).filter((item) => item.group === 'primary'),
    supporting: (artifacts || []).filter((item) => item.group === 'supporting'),
    debug: (artifacts || []).filter((item) => item.group === 'debug'),
  }
  return (
    <div className="detail-stack">
      {Object.entries(groups).map(([group, items]) => (
        <div className="subpanel" key={group}>
          <div className="eyebrow">{group === 'primary' ? 'Primary Artifacts' : group === 'supporting' ? 'Supporting Sources' : 'Diagnostics'}</div>
          {items.length ? (
            <div className="detail-stack">
              {items.map((artifact) => (
                <div className="review-artifact-card" key={`${artifact.kind}-${artifact.target}`}>
                  <div className="activity-meta">
                    <Badge tone={artifact.exists ? 'success' : artifact.external ? 'neutral' : 'warning'}>{artifact.kind}</Badge>
                    <span>{artifact.exists ? 'available' : artifact.external ? 'external' : 'missing'}</span>
                  </div>
                  <strong>{artifact.label}</strong>
                  <div className="cell-meta">{artifact.relative_path || artifact.target || '-'}</div>
                  {artifact.href ? <a href={artifact.href} rel="noreferrer" target="_blank">Open artifact</a> : <div className="detail-line">Artifact path recorded, but no openable file is available.</div>}
                </div>
              ))}
            </div>
          ) : (
            <div className="detail-line">{group === 'primary' ? 'No primary artifacts are available for this application yet.' : 'No entries recorded for this group.'}</div>
          )}
        </div>
      ))}
    </div>
  )
}

function ReviewHistoryTab({ history }) {
  return (
    <div className="detail-stack">
      {(history || []).length ? (
        history.map((entry, index) => (
          <article className="activity-item" key={`${entry.timestamp || index}-${entry.type || index}`}>
            <div className="activity-meta">
              <Badge tone={toneFor(entry.type || entry.summary)}>{entry.type || 'review.event'}</Badge>
              <span>{entry.actor || 'operator'}</span>
              <span>{entry.timestamp ? formatDate(entry.timestamp) : '-'}</span>
            </div>
            <strong>{entry.summary || 'Review event recorded.'}</strong>
            {entry.metadata?.reason ? <div className="cell-meta">Note: {entry.metadata.reason}</div> : null}
            {entry.metadata?.updated_count ? <div className="cell-meta">Updated answers: {formatNumber(entry.metadata.updated_count)}</div> : null}
          </article>
        ))
      ) : <div className="subpanel"><div className="detail-line">No review history recorded yet.</div></div>}
    </div>
  )
}

function ReviewQueueInbox({ items, selectedId, onSelect }) {
  return (
    <div className="table-wrap review-table-wrap">
      <table className="data-table review-data-table review-compact-table">
        <thead>
          <tr>
            <th>Company / Role</th>
            <th>Review State</th>
            <th>Needs Attention</th>
            <th>Next Step</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const summary = item.review_summary || {}
            const handoff = item.manual_handoff || {}
            return (
              <tr
                className={`review-row-table ${selectedId === item.application_id ? 'selected' : ''}`.trim()}
                key={item.application_id}
                onClick={() => onSelect(item.application_id)}
              >
                <td>
                  <div className="review-cell-stack">
                    <strong>{item.company}</strong>
                    <div className="cell-meta">{item.title}</div>
                    {reviewQueueSupportingMeta(item) ? <div className="cell-meta">{reviewQueueSupportingMeta(item)}</div> : null}
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={toneFor(summary.severity || item.review_status || item.status)}>{item.review_status || item.status}</Badge>
                    <div className="cell-meta">{handoff.active ? 'Manual handoff active' : item.status || '-'}</div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={summary.blocker_count ? 'danger' : summary.warning_count ? 'warning' : handoff.active ? 'warning' : 'success'}>
                      {reviewQueueAttentionSummary(item)}
                    </Badge>
                    <div className="cell-meta">
                      {summary.unresolved_question_count
                        ? `${formatNumber(summary.unresolved_question_count)} question${summary.unresolved_question_count === 1 ? '' : 's'} still open`
                        : handoff.active
                          ? `Watching ${formatNumber(handoff.pending_count || 0)} pending field${safeNumber(handoff.pending_count) === 1 ? '' : 's'}`
                          : 'Nothing blocking right now'}
                    </div>
                  </div>
                </td>
                <td>
                  <div className="review-cell-stack">
                    <Badge tone={reviewActionTone(summary.next_action)}>{reviewActionLabel(summary.next_action)}</Badge>
                    <div className="cell-meta">{summary.next_action_reason || '-'}</div>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ReviewMoreMenu({ onSelectAction }) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const handlePointerDown = (event) => {
      if (!menuRef.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handlePointerDown)
    return () => document.removeEventListener('mousedown', handlePointerDown)
  }, [open])

  return (
    <div className="review-more-menu" ref={menuRef}>
      <button
        aria-expanded={open}
        className="button button-ghost review-more-trigger"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        More
      </button>
      {open ? (
        <div className="review-more-panel">
          <button className="button button-ghost" type="button" onClick={() => { setOpen(false); onSelectAction('mark_submitted') }}>Mark As Submitted</button>
          <button className="button button-ghost" type="button" onClick={() => { setOpen(false); onSelectAction('reject') }}>Reject</button>
        </div>
      ) : null}
    </div>
  )
}

function ReviewDisclosure({ title, summary, open, onToggle, children }) {
  return (
    <section className={`review-section review-disclosure ${open ? 'open' : ''}`.trim()}>
      <button aria-expanded={open} className="review-disclosure-toggle" type="button" onClick={onToggle}>
        <div>
          <strong>{title}</strong>
          {summary ? <div className="cell-meta">{summary}</div> : null}
        </div>
        <span className="review-disclosure-state">{open ? 'Hide' : 'Show'}</span>
      </button>
      {open ? <div className="review-disclosure-body">{children}</div> : null}
    </section>
  )
}

function ReviewQuestionsPanel({ questions, selectedId, answers, setAnswers, onSave }) {
  const unresolved = (questions || []).filter((question) => question.needs_user_input)
  const resolved = (questions || []).filter((question) => !question.needs_user_input)
  const [showResolved, setShowResolved] = useState(false)
  return (
    <div className="detail-stack">
      {unresolved.length ? (
        <div className="detail-stack">
          {unresolved.map((question) => {
            const key = `${selectedId}::${question.question_id}`
            const options = questionOptions(question)
            const value = Object.prototype.hasOwnProperty.call(answers, key) ? answers[key] : hydrateAnswerDraft(question, question.existing_answer ?? '')
            const isCheckboxGroup = question.widget_type === 'checkbox_group' && options.length > 0
            const isSelect = options.length > 0 && !isCheckboxGroup
            const selectedValues = Array.isArray(value) ? value : []
            return (
              <div className="question-card" key={question.question_id}>
                <div className="activity-meta">
                  <Badge tone={question.required ? 'warning' : 'neutral'}>{question.required ? 'required' : 'optional'}</Badge>
                  <span>{question.question_type || question.widget_type || 'question'}</span>
                  <span>{question.verification_status || 'needs_user_input'}</span>
                </div>
                <strong>{question.prompt_text}</strong>
                <div className="answer-row">
                  {isCheckboxGroup ? (
                    <div className="detail-stack checkbox-stack">
                      {options.map((option) => (
                        <label key={`${question.question_id}-${option.label}`}>
                          <input
                            checked={selectedValues.includes(option.label)}
                            onChange={(event) => setAnswers((current) => {
                              const base = Object.prototype.hasOwnProperty.call(current, key) ? current[key] : selectedValues
                              const selected = Array.isArray(base) ? [...base] : []
                              return {
                                ...current,
                                [key]: event.target.checked
                                  ? dedupeStrings([...selected, option.label])
                                  : selected.filter((entry) => entry !== option.label),
                              }
                            })}
                            type="checkbox"
                          /> {option.label}
                        </label>
                      ))}
                    </div>
                  ) : isSelect ? (
                    <select value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))}>
                      <option value="">Select answer</option>
                      {options.map((option) => <option key={`${question.question_id}-${option.value}-${option.label}`} value={option.label}>{option.label}</option>)}
                    </select>
                  ) : (
                    <input value={String(value ?? '')} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))} placeholder="Type answer" />
                  )}
                  <button className="button button-primary" type="button" onClick={() => onSave(question)}>Save Answer</button>
                </div>
                <div className="cell-meta">Saved answers are reused automatically the next time this prompt appears.</div>
              </div>
            )
          })}
        </div>
      ) : <div className="detail-line">No unanswered questions right now.</div>}
      {resolved.length ? (
        <div className="detail-stack">
          <button className="button button-ghost review-inline-toggle" type="button" onClick={() => setShowResolved((current) => !current)}>
            {showResolved ? 'Hide Saved Answers' : `Show Saved Answers (${formatNumber(resolved.length)})`}
          </button>
          {showResolved ? (
            <div className="detail-stack">
              {resolved.map((question) => (
                <div className="review-resolved-answer" key={question.question_id}>
                  <div className="detail-line"><strong>{question.prompt_text}</strong></div>
                  <div className="cell-meta">{question.existing_answer || 'Saved with no visible answer text.'}</div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function ReviewNeedsAttentionPanel({ detail }) {
  const application = detail?.application || {}
  const summary = detail?.summary || {}
  const blockers = detail?.blockers || []
  const warningBlockers = blockers.filter((blocker) => blocker?.category === 'warning')
  const primaryBlockers = blockers.filter((blocker) => blocker?.category !== 'warning')
  const compactStats = [
    { label: 'Questions', value: formatNumber(summary.unresolved_question_count || 0) },
    { label: 'Missing', value: formatNumber(summary.missing_required_count || 0) },
    { label: 'Warnings', value: formatNumber(summary.warning_count || 0) },
  ]
  return (
    <section className="review-section">
      <div className="review-section-heading">
        <div>
          <div className="eyebrow">Needs Attention</div>
          <h3>What needs your attention</h3>
        </div>
        <Badge tone={reviewActionTone(summary.next_action)}>{reviewActionLabel(summary.next_action)}</Badge>
      </div>
      <p className="section-copy">{summary.next_action_reason || `Review ${application.company || 'this application'} and decide the next step.`}</p>
      <div className="review-stat-strip">
        {compactStats.map((item) => (
          <div className="review-stat-pill" key={item.label}>
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
      {primaryBlockers.length ? (
        <div className="subpanel">
          <div className="eyebrow">Blockers</div>
          <div className="detail-stack">
            {primaryBlockers.map((blocker, index) => (
              <div className="review-chip-row" key={`${blockerLabel(blocker)}-${index}`}>
                <Badge tone="danger">{blocker?.category || 'blocker'}</Badge>
                <div className="cell-meta">{blockerLabel(blocker)}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {warningBlockers.length ? (
        <div className="subpanel">
          <div className="eyebrow">Warnings</div>
          <div className="detail-stack">
            {warningBlockers.map((blocker, index) => (
              <div className="review-chip-row" key={`${blockerLabel(blocker)}-${index}`}>
                <Badge tone="warning">warning</Badge>
                <div className="cell-meta">{blockerLabel(blocker)}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {!primaryBlockers.length && !warningBlockers.length ? (
        <div className="subpanel">
          <div className="detail-line">Nothing is blocked right now. You can review the documents, continue with manual input, or apply.</div>
        </div>
      ) : null}
    </section>
  )
}

function ReviewDocumentsPanel({ detail }) {
  const artifacts = detail?.artifacts || []
  const primaryDocuments = [
    { label: 'Resume PDF', artifact: reviewArtifactForKinds(artifacts, 'resume_pdf') },
    { label: 'Cover Letter PDF', artifact: reviewArtifactForKinds(artifacts, 'cover_letter_pdf') },
    { label: 'Evaluation Report', artifact: reviewArtifactForKinds(artifacts, 'evaluation_report') },
    { label: 'Job Posting', artifact: reviewArtifactForKinds(artifacts, 'job_posting') },
  ]
  const supporting = artifacts.filter((item) => item.group === 'supporting')
  return (
    <div className="detail-stack">
      <div className="review-document-list">
        {primaryDocuments.map((item) => (
          <div className="review-document-row" key={item.label}>
            <div>
              <strong>{item.label}</strong>
              <div className="cell-meta">{item.artifact?.relative_path || item.artifact?.target || `No ${item.label.toLowerCase()} recorded yet.`}</div>
            </div>
            <div className="review-document-actions">
              <Badge tone={item.artifact?.exists ? 'success' : item.artifact?.external ? 'neutral' : 'warning'}>
                {item.artifact?.exists ? 'available' : item.artifact?.external ? 'external' : 'missing'}
              </Badge>
              {item.artifact?.href ? <a href={item.artifact.href} rel="noreferrer" target="_blank">Open</a> : null}
            </div>
          </div>
        ))}
      </div>
      {supporting.length ? (
        <div className="subpanel">
          <div className="eyebrow">Additional Sources</div>
          <div className="detail-stack">
            {supporting.map((artifact) => (
              <div className="detail-line" key={`${artifact.kind}-${artifact.target}`}>
                <strong>{artifact.label}:</strong> {artifact.href ? <a href={artifact.href} rel="noreferrer" target="_blank">Open</a> : artifact.relative_path || artifact.target || 'Recorded'}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}

function ReviewManualHandoffPanel({ manualHandoffWatch }) {
  const recentAnswers = manualHandoffWatch?.recent_answers || []
  const [showChanges, setShowChanges] = useState(false)
  return (
    <div className="detail-stack">
      <div className="review-stat-strip">
        <div className="review-stat-pill"><span>Status</span><strong>{manualHandoffWatch?.status || (manualHandoffWatch?.active ? 'watching' : 'idle')}</strong></div>
        <div className="review-stat-pill"><span>Last Sync</span><strong>{manualHandoffWatch?.last_synced_at ? formatDate(manualHandoffWatch.last_synced_at) : '-'}</strong></div>
        <div className="review-stat-pill"><span>Learned</span><strong>{formatNumber(manualHandoffWatch?.synced_question_count || 0)}</strong></div>
      </div>
      <div className="subpanel">
        <div className="eyebrow">Tracked Page</div>
        {manualHandoffWatch?.last_page_url ? (
          <a href={manualHandoffWatch.last_page_url} rel="noreferrer" target="_blank">{manualHandoffWatch.last_page_url}</a>
        ) : <div className="detail-line">No parked browser page URL recorded yet.</div>}
      </div>
      <div className="detail-line">
        {manualHandoffWatch?.active
          ? `The parked application is still being watched. ${formatNumber(manualHandoffWatch?.pending_count || 0)} text field${safeNumber(manualHandoffWatch?.pending_count) === 1 ? '' : 's'} are still settling.`
          : 'No active manual handoff session is being watched right now.'}
      </div>
      {recentAnswers.length ? (
        <div className="detail-stack">
          <button className="button button-ghost review-inline-toggle" type="button" onClick={() => setShowChanges((current) => !current)}>
            {showChanges ? 'Hide Learned Changes' : `Show Learned Changes (${formatNumber(recentAnswers.length)})`}
          </button>
          {showChanges ? (
            <div className="table-wrap">
              <table className="data-table review-data-table handoff-change-table">
                <thead>
                  <tr>
                    <th>Question</th>
                    <th>Previous</th>
                    <th>New</th>
                    <th>Change Type</th>
                  </tr>
                </thead>
                <tbody>
                  {recentAnswers.map((item, index) => (
                    <tr key={`${item.question_id || item.prompt_text}-${index}`}>
                      <td>{item.prompt_text || item.question_id || '-'}</td>
                      <td>{item.previous_answer || '-'}</td>
                      <td>{item.answer_text || '-'}</td>
                      <td><Badge tone={item.filled_blank ? 'success' : 'warning'}>{item.change_type || (item.filled_blank ? 'filled_blank' : 'corrected_answer')}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function ReviewAdvancedPanel({ detail }) {
  const history = detail?.history || []
  const summary = detail?.summary || {}
  const reportMarkdown = detail?.report_markdown || ''
  return (
    <div className="detail-stack">
      <div className="subpanel">
        <div className="eyebrow">Screening And Classification</div>
        <div className="detail-stack">
          <div className="detail-line">Screening: {summary.screening_status || detail?.job?.screening_status || '-'}</div>
          <div className="detail-line">ATS family: {summary.classification?.ats_family || detail?.job?.ats_family || '-'}</div>
          <div className="detail-line">Board family: {summary.classification?.board_family || detail?.job?.board_family || '-'}</div>
          <div className="detail-line">Automation tier: {summary.classification?.automation_tier || detail?.job?.automation_tier || '-'}</div>
          <div className="detail-line">Low confidence answers: {formatNumber(summary.low_confidence_count || 0)}</div>
          <div className="detail-line">Ungrounded answers: {formatNumber(summary.ungrounded_count || 0)}</div>
          {summary.hard_reject_reason ? <div className="detail-line">Hard reject reason: {summary.hard_reject_reason}</div> : null}
          {summary.auth_reject_reason ? <div className="detail-line">Auth reject reason: {summary.auth_reject_reason}</div> : null}
        </div>
      </div>
      {history.length ? (
        <div className="detail-stack">
          {history.map((entry, index) => (
            <article className="activity-item" key={`${entry.timestamp || index}-${entry.type || index}`}>
              <div className="activity-meta">
                <Badge tone={toneFor(entry.type || entry.summary)}>{entry.type || 'review.event'}</Badge>
                <span>{entry.actor || 'operator'}</span>
                <span>{entry.timestamp ? formatDate(entry.timestamp) : '-'}</span>
              </div>
              <strong>{entry.summary || 'Review event recorded.'}</strong>
              {entry.metadata?.reason ? <div className="cell-meta">Note: {entry.metadata.reason}</div> : null}
              {entry.metadata?.updated_count ? <div className="cell-meta">Updated answers: {entry.metadata.updated_count}</div> : null}
            </article>
          ))}
        </div>
      ) : <div className="subpanel"><div className="detail-line">No review history recorded yet.</div></div>}
      <div className="subpanel">
        <div className="eyebrow">Report Output</div>
        <pre className="report-block">{reportMarkdown || 'No report available.'}</pre>
      </div>
    </div>
  )
}

function ReviewPage({ live }) {
  const review = usePolledJson('/api/review/queue', 7000)
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('application_id') || ''
  const activeSectionFromUrl = reviewSectionFromTab(searchParams.get('tab'))
  const detail = usePolledJson(selectedId ? `/api/applications/${selectedId}` : '/api/review/queue?limit=1', 7000)
  const [notice, setNotice] = useState('')
  const [answers, setAnswers] = useState({})
  const [queueSearch, setQueueSearch] = useState('')
  const [queueFilter, setQueueFilter] = useState('all')
  const [confirmAction, setConfirmAction] = useState('')
  const [actionNote, setActionNote] = useState('')
  const [sectionOverrides, setSectionOverrides] = useState({})
  const searchInputRef = useRef(null)
  const reviewItems = review.data?.items || []
  const manualHandoffWatch = detail.data?.manual_handoff_watch || detail.data?.submission?.result?.manual_handoff_watch || {}
  const unresolvedQuestions = (detail.data?.questions || []).filter((question) => question.needs_user_input)
  const handoffRecentlyActive = Boolean(manualHandoffWatch?.active || manualHandoffWatch?.last_synced_at || (manualHandoffWatch?.recent_answers || []).length)
  const sectionDefaults = useMemo(() => ({
    questions: activeSectionFromUrl === 'questions' || unresolvedQuestions.length > 0,
    documents: activeSectionFromUrl === 'documents',
    handoff: activeSectionFromUrl === 'handoff' || handoffRecentlyActive,
    advanced: activeSectionFromUrl === 'advanced',
  }), [activeSectionFromUrl, handoffRecentlyActive, unresolvedQuestions.length])

  const filteredItems = useMemo(() => {
    return [...reviewItems]
      .filter((item) => queueMatchesSearch(item, queueSearch))
      .filter((item) => reviewQueueMatchesFilter(item, queueFilter))
      .sort(reviewSortComparator())
  }, [queueFilter, queueSearch, reviewItems])

  const queueCounts = useMemo(() => ({
    all: reviewItems.length,
    needs_input: reviewItems.filter((item) => reviewQueueMatchesFilter(item, 'needs_input')).length,
    manual_handoff: reviewItems.filter((item) => reviewQueueMatchesFilter(item, 'manual_handoff')).length,
    ready: reviewItems.filter((item) => reviewQueueMatchesFilter(item, 'ready')).length,
  }), [reviewItems])

  function setReviewParam(key, value) {
    const nextParams = new URLSearchParams(searchParams)
    if (value) nextParams.set(key, value)
    else nextParams.delete(key)
    setSearchParams(nextParams, { replace: true })
  }

  function selectApplication(applicationId) {
    setReviewParam('application_id', applicationId)
  }

  function openConfirm(action) {
    setConfirmAction(action)
    setActionNote('')
  }

  function isSectionOpen(sectionKey) {
    if (Object.prototype.hasOwnProperty.call(sectionOverrides, sectionKey)) return sectionOverrides[sectionKey]
    return sectionDefaults[sectionKey] || false
  }

  function toggleSection(sectionKey) {
    const nextOpen = !isSectionOpen(sectionKey)
    setSectionOverrides((current) => ({ ...current, [sectionKey]: nextOpen }))
    if (nextOpen) {
      const tab = reviewTabForSection(sectionKey)
      setReviewParam('tab', tab === 'summary' ? '' : tab)
      return
    }
    if (activeSectionFromUrl === sectionKey) {
      setReviewParam('tab', '')
    }
  }

  useEffect(() => {
    if (!review.data) return
    const queueIds = filteredItems.map((item) => item.application_id)
    if (!queueIds.length) {
      if (selectedId) selectApplication('')
      return
    }
    if (!selectedId || !queueIds.includes(selectedId)) {
      selectApplication(queueIds[0])
    }
  }, [filteredItems, selectedId, review.data])

  useEffect(() => {
    setSectionOverrides({})
  }, [selectedId])

  useEffect(() => {
    const onKeyDown = (event) => {
      if (isEditableShortcutTarget(event.target)) return
      if (!filteredItems.length) return
      const currentIndex = Math.max(0, filteredItems.findIndex((item) => item.application_id === selectedId))
      if (event.key === '/') {
        event.preventDefault()
        searchInputRef.current?.focus()
        return
      }
      if (event.key === 'j') {
        event.preventDefault()
        selectApplication(filteredItems[Math.min(currentIndex + 1, filteredItems.length - 1)]?.application_id || filteredItems[0].application_id)
        return
      }
      if (event.key === 'k') {
        event.preventDefault()
        selectApplication(filteredItems[Math.max(currentIndex - 1, 0)]?.application_id || filteredItems[0].application_id)
        return
      }
      if (!selectedId) return
      if (event.key === 'a') void takeAction('approve')
      if (event.key === 'o') void takeAction('request_input')
      if (event.key === 's') void takeAction('sync_manual_input')
      if (event.key === 'm') openConfirm('mark_submitted')
      if (event.key === 'r') openConfirm('reject')
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [filteredItems, selectedId])

  async function takeAction(action, reason = '') {
    if (!selectedId) return
    try {
      const result = await requestJson('/api/review/action', {
        method: 'POST',
        body: JSON.stringify({ application_id: selectedId, action, reason: reason || undefined }),
        timeoutMs: 120_000,
      })
      if (action === 'sync_manual_input') {
        if (!result?.page_found) {
          setNotice('Could not find an open parked application tab to sync yet. Open the manual handoff page first, then retry.')
        } else if (result?.synced_count) {
          const filledBlankCount = Number(result?.filled_blank_count || 0)
          const correctedAnswerCount = Number(result?.corrected_answer_count || 0)
          setNotice(`Synced ${result.synced_count} browser answer${result.synced_count === 1 ? '' : 's'}. ${filledBlankCount} filled blank field${filledBlankCount === 1 ? '' : 's'}, ${correctedAnswerCount} corrected existing answer${correctedAnswerCount === 1 ? '' : 's'}.`)
        } else {
          setNotice('Checked the parked application page. There are no new manual answers to save yet.')
        }
      } else if (result?.manual_submitted) {
        setNotice('Marked this application as submitted. It now counts toward submitted totals and leaves the review queue.')
      } else if (action === 'reject') {
        setNotice('Rejected this application and removed it from the active review queue.')
      } else if (result?.manual_handoff_opened && action === 'request_input') {
        setNotice('Opened the partially filled application for manual completion. No submit attempt was made.')
      } else if (result?.manual_handoff_opened) {
        setNotice('Submission is still blocked. Opened the partially filled application for manual completion instead of submitting.')
      } else if (result?.blocked) {
        setNotice('Submission is still blocked. The page could not be kept open automatically, so answer the saved blockers here and retry.')
      } else {
        setNotice(`Review action applied: ${reviewActionLabel(action)}`)
      }
      setConfirmAction('')
      setActionNote('')
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
      <Section eyebrow="Review Queue" title="Applications To Review" description="A compact inbox for the applications that still need your attention.">
        <DataState error={review.error} loading={review.loading} empty={!reviewItems.length} emptyLabel="No review items." emptyDetail="Discovery and drafting must create application records before review can begin.">
          <div className="section-stack">
            <div className="review-queue-controls">
              <label className="review-search">
                <span>Search</span>
                <input ref={searchInputRef} value={queueSearch} onChange={(event) => setQueueSearch(event.target.value)} placeholder="Search company, role, blocker, or next step" />
              </label>
              <div className="review-filter-row" aria-label="Review queue filters">
                {REVIEW_QUEUE_FILTERS.map((item) => (
                  <button
                    className={`review-filter-pill ${queueFilter === item.key ? 'active' : ''}`.trim()}
                    key={item.key}
                    type="button"
                    onClick={() => setQueueFilter(item.key)}
                  >
                    {item.label} <span>{formatNumber(queueCounts[item.key] || 0)}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="cell-meta">Showing {formatNumber(filteredItems.length)} of {formatNumber(reviewItems.length)} applications.</div>
            <DataState error="" loading={false} empty={!filteredItems.length} emptyLabel="No filtered review items." emptyDetail="Adjust the search or queue filters to show more applications.">
              <ReviewQueueInbox items={filteredItems} selectedId={selectedId} onSelect={selectApplication} />
            </DataState>
          </div>
        </DataState>
      </Section>
      <Section eyebrow="Review Workspace" title="Selected Application" description="A focused workspace for clearing blockers, finishing handoffs, and moving the application forward.">
        <DataState error={detail.error} loading={detail.loading} empty={!detail.data?.application} emptyLabel="Choose an application." emptyDetail="Select an application from the review queue to inspect details.">
          <InlineNotice message={notice} tone={toneFor(notice)} />
          <div className="review-detail-shell">
            <div className="review-detail-header review-summary-card">
              <div className="review-detail-title">
                <div className="eyebrow">Application Summary</div>
                <h3>{detail.data?.application?.company} / {detail.data?.application?.role}</h3>
                <p className="section-copy">{detail.data?.summary?.next_action_reason || 'Review the open questions and decide whether to continue, hand off, or apply.'}</p>
              </div>
              <div className="review-header-metrics">
                <div className="review-header-line">
                  <Badge tone={toneFor(detail.data?.summary?.severity || detail.data?.application?.status)}>{detail.data?.application?.status || '-'}</Badge>
                  <Badge tone={toneFor(detail.data?.submission?.status)}>{detail.data?.submission?.status || 'not_prepared'}</Badge>
                  {reviewSourceMeta(detail.data) ? <Badge tone="neutral">{reviewSourceMeta(detail.data)}</Badge> : null}
                </div>
                {detail.data?.application?.score != null || detail.data?.application?.grade ? (
                  <div className="cell-meta">
                    {[
                      detail.data?.application?.score != null ? `Score ${detail.data.application.score}` : '',
                      detail.data?.application?.grade ? `Grade ${detail.data.application.grade}` : '',
                    ].filter(Boolean).join(' / ')}
                  </div>
                ) : null}
              </div>
              <div className="review-action-bar">
                <button className="button button-primary" type="button" onClick={() => takeAction('approve')}>Approve / Apply</button>
                <button className="button button-ghost" type="button" onClick={() => takeAction('request_input')}>Open For Manual Input</button>
                <button className="button button-ghost" type="button" onClick={() => takeAction('sync_manual_input')}>Sync Browser Changes</button>
                <ReviewMoreMenu onSelectAction={openConfirm} />
              </div>
              {confirmAction ? <div className="review-confirm-panel"><div className="eyebrow">{reviewActionLabel(confirmAction)}</div><textarea rows="3" value={actionNote} onChange={(event) => setActionNote(event.target.value)} placeholder={confirmAction === 'mark_submitted' ? 'Optional note about how you submitted it manually' : 'Optional note about why this application is being rejected'} /><div className="action-row"><button className="button button-primary" type="button" onClick={() => takeAction(confirmAction, actionNote)}>{confirmAction === 'mark_submitted' ? 'Confirm Manual Submission' : 'Confirm Reject'}</button><button className="button button-ghost" type="button" onClick={() => { setConfirmAction(''); setActionNote('') }}>Cancel</button></div></div> : null}
              <div className="cell-meta">Use manual input when the form is mostly filled and just needs your help. Sync afterward to save what you entered for reuse.</div>
            </div>
            <ReviewNeedsAttentionPanel detail={detail.data} />
            <ReviewDisclosure
              title="Questions"
              summary={unresolvedQuestions.length ? `${formatNumber(unresolvedQuestions.length)} answer${unresolvedQuestions.length === 1 ? '' : 's'} still need your input.` : 'Review saved answers or add anything still missing.'}
              open={isSectionOpen('questions')}
              onToggle={() => toggleSection('questions')}
            >
              <ReviewQuestionsPanel questions={detail.data?.questions || []} selectedId={selectedId} answers={answers} setAnswers={setAnswers} onSave={submitReviewAnswer} />
            </ReviewDisclosure>
            <ReviewDisclosure
              title="Documents & Links"
              summary="Open the generated documents, evaluation report, and the original job posting."
              open={isSectionOpen('documents')}
              onToggle={() => toggleSection('documents')}
            >
              <ReviewDocumentsPanel detail={detail.data} />
            </ReviewDisclosure>
            <ReviewDisclosure
              title="Manual Handoff"
              summary={handoffRecentlyActive ? 'A manual handoff is active or was recently synced.' : 'Only open this when you need manual takeover details.'}
              open={isSectionOpen('handoff')}
              onToggle={() => toggleSection('handoff')}
            >
              <ReviewManualHandoffPanel manualHandoffWatch={manualHandoffWatch} />
            </ReviewDisclosure>
            <ReviewDisclosure
              title="Advanced"
              summary="History, diagnostics, and lower-level review context."
              open={isSectionOpen('advanced')}
              onToggle={() => toggleSection('advanced')}
            >
              <ReviewAdvancedPanel detail={detail.data} />
            </ReviewDisclosure>
          </div>
        </DataState>
      </Section>
    </div>
  )
}



function Layout({ children }) {
  const location = useLocation()
  const activePath = navPathForLocation(location.pathname)
  return <div className="app-shell"><header className="topbar"><div><div className="brand-mark">Find My Job</div><h1>Operator Console</h1></div><nav className="topnav">{NAV_ITEMS.map((item) => <Link className={activePath === item.to ? 'active' : ''} key={item.to} to={item.to}>{item.label}</Link>)}</nav></header><main className="page-shell">{children}</main></div>
}

export function App() {
  const live = useLiveConsole()
  const operator = useMemo(() => deriveOperatorState(live.snapshot, live.connection, live.lastSnapshotAt), [live.connection, live.lastSnapshotAt, live.snapshot])

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<DashboardPage operator={operator} live={live} />} />
        <Route path="/setup" element={<Navigate to="/settings" replace />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/autopilot" element={<AutopilotPage operator={operator} live={live} />} />
        <Route path="/daily" element={<AutopilotPage operator={operator} live={live} />} />
        <Route path="/review" element={<ReviewPage operator={operator} live={live} />} />
        <Route path="/runs" element={<Navigate to="/" replace />} />
        <Route path="/training" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}

export { AutopilotPage, DashboardPage, ReviewPage, SettingsPage, requestJson }
