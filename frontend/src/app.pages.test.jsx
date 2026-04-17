import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BrowserRouter } from 'react-router-dom'

import { App, AutopilotPage, DashboardPage, ReviewPage, RunsPage, SettingsPage, SetupPage, deriveOperatorState } from './app.jsx'


function buildOperator() {
  return deriveOperatorState(
    {
      state: {
        run_type: 'idle',
        status: 'idle',
        stage: 'idle',
        latest_operator_message: 'No active run.',
        stats: {},
      },
      events: [],
    },
    'connected',
    Date.now(),
  )
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return JSON.parse(JSON.stringify(payload))
    },
    async text() {
      return JSON.stringify(payload)
    },
  }
}

function createSettingsPayload(overrides = {}) {
  return {
    runtime_model: {
      provider: 'lmstudio',
      transport: 'local_http',
      base_url: 'http://127.0.0.1:1234',
      api_key_env: null,
      model: 'runtime-qwen',
      temperature: 0.2,
      max_tokens: 8192,
      preferred_context_window: 131072,
      local: true,
      command: [],
      working_dir: '',
    },
    local_model: {
      provider: 'lmstudio',
      transport: 'local_http',
      base_url: 'http://127.0.0.1:1234',
      api_key_env: null,
      model: 'runtime-qwen',
      temperature: 0.2,
      max_tokens: 8192,
      preferred_context_window: 131072,
      local: true,
      command: [],
      working_dir: '',
    },
    autonomous: {
      enabled: true,
      submit_enabled: true,
      default_submit_mode: 'auto_submit',
      ready_to_apply_threshold: 5,
      browser_mode: 'headed',
      browser_attach_enabled: false,
      browser_cdp_url: 'http://127.0.0.1:9222',
      max_open_tabs: 6,
      daily_submit_cap: 10,
      per_company_daily_cap: 3,
      production_sources: ['greenhouse'],
      captcha_strategy: 'manual',
      captcha_provider: '2captcha',
      captcha_api_key_env: 'CAPTCHA_API_KEY',
      captcha_solve_timeout_seconds: 300,
    },
    portals: {
      sources: {
        greenhouse: { enabled: true, boards: ['acme'], seed_urls: [], seed_domains: [] },
        lever: { enabled: false, boards: ['plaid'], seed_urls: [], seed_domains: [] },
        ashby: { enabled: false, boards: ['notion'], seed_urls: [], seed_domains: [] },
      },
      tracked_companies: [],
    },
    tracked_companies: [],
    advanced_models: {
      profiles: [],
      role_bindings: {},
    },
    last_model_checks: {
      'runtime-model': {
        ok: true,
        classification: 'ok',
        checked_at: '2026-04-13T12:00:00Z',
      },
    },
    model_strategy: {
      mode: 'lm_studio_local',
      provider: 'lmstudio',
      transport: 'local_http',
      model: 'runtime-qwen',
      base_url: 'http://127.0.0.1:1234',
      launch_transport_mix: 'LM Studio local routing',
    },
    readiness: {
      config_validation: { overall_status: 'pass' },
      doctor: { overall_status: 'pass' },
      launch_check: { overall_status: 'pass' },
      findings: [],
    },
    drafting_strategy: { renderer: 'chatgpt_download' },
    chatgpt_drafting: {
      enabled: true,
      renderer: 'chatgpt_download',
      gpt_url: 'https://chatgpt.com/g/custom-test',
      completion_start_marker: '[[PDF_OUTPUT_READY]]',
      completion_end_marker: '[[PDF_OUTPUT_COMPLETE]]',
      browser: {
        profile_dir: '.fmj/browser/chatgpt-profile',
        downloads_dir: '.fmj/runtime/chatgpt-downloads',
        browser_mode: 'attached',
        browser_cdp_url: 'http://127.0.0.1:9333',
        launch_if_missing: true,
        profile_dir_exists: true,
      },
      timeout_seconds: 240,
      prompt_submit_delay_ms: 300,
      download_timeout_seconds: 120,
      max_parallel_jobs: 4,
      launch_status: { last_browser_launch_ok: true, last_browser_launch_at: '2026-04-13T11:00:00Z' },
      last_result: { success: true, application_id: '001' },
      last_error: '',
    },
    ...overrides,
  }
}

function installFetchMock(handler) {
  const calls = []
  const fetchMock = vi.fn(async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url
    const method = init?.method || 'GET'
    const body = init?.body ?? null
    calls.push({ url, method, body })
    const response = await handler({ url, method, body })
    if (!response) {
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    }
    if (typeof response.json === 'function') {
      return response
    }
    return jsonResponse(response.body ?? response, response.status ?? 200)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

class MockEventSource {
  constructor(url) {
    this.url = url
    this.listeners = {}
    this.onopen = null
    this.onerror = null
  }

  addEventListener(type, listener) {
    this.listeners[type] = listener
  }

  removeEventListener(type) {
    delete this.listeners[type]
  }

  close() {}
}

function renderWithRouter(ui, route = '/') {
  window.history.pushState({}, '', route)
  return render(
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      {ui}
    </BrowserRouter>
  )
}


describe('console pages', () => {
  beforeEach(() => {
    vi.useRealTimers()
    vi.stubGlobal('EventSource', MockEventSource)
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('dashboard stays read-first and surfaces recent runs without execution controls', async () => {
    const live = { error: '', refresh: vi.fn().mockResolvedValue({}) }
    installFetchMock(async ({ url, method }) => {
      if (url === '/api/dashboard') {
        return {
          snapshot: { counts: { inbox: 2, applications: 1 } },
          autonomous: { queue_depth: 1, blocked_by_questions: 0, ready_to_apply: 1, ready_to_apply_threshold: 5, unresolved_prompts: 0 },
          jobs_table: {
            items: [
              { job_id: 'job-100', company: 'Acme', role: 'Backend Engineer', status: 'Ready to Submit', source: 'greenhouse', application_id: '001' },
            ],
          },
        }
      }
      if (url === '/api/runs/history') {
        return {
          items: [
            {
              run_id: 'run-001',
              run_type: 'autonomous',
              status: 'completed',
              started_at: '2026-04-13T12:00:00Z',
              completed_at: '2026-04-13T12:15:00Z',
              submitted_count: 2,
              failed_count: 1,
              processed_count: 8,
              evaluated_count: 5,
            },
          ],
        }
      }
      return null
    })

    renderWithRouter(<DashboardPage operator={buildOperator()} live={live} />)

    await screen.findByText('Overview And Health')
    expect(screen.getByRole('link', { name: 'Open Autopilot' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open Review' })).toBeTruthy()
    expect(screen.getByText('run-001')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Discover Jobs' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Full Run' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull()
  })

  it('setup compatibility view lands on settings readiness and resets operational state', async () => {
    const { calls } = installFetchMock(async ({ url, method }) => {
      if (url === '/api/settings') return createSettingsPayload()
      if (url.startsWith('/api/settings/models/available')) {
        return { models: [{ id: 'runtime-qwen', label: 'runtime-qwen' }], count: 1, source: 'lmstudio', note: '1 models available from lmstudio.' }
      }
      if (url === '/api/setup/readiness') {
        return {
          overall_status: 'pass',
          config_validation: { overall_status: 'pass' },
          doctor: { overall_status: 'pass' },
          launch_check: { overall_status: 'pass' },
          sources: { greenhouse: {}, lever: {}, ashby: {} },
          automation: { submit_enabled: true },
          profile_surface: {
            mode: 'sample_mode',
            configured: false,
            local_path: '.fmj/local-overrides/filefirst/user-profile.yml',
            local_template_path: '.fmj/local-overrides/filefirst/user-profile.template.yml',
            public_template_path: 'templates/user-profile.local.example.yml',
            active_advanced_paths: [],
          },
          findings: [{ key: 'sources.greenhouse.targets', status: 'pass', summary: 'Greenhouse ready.' }],
        }
      }
      if (url === '/api/workspace/reset-operational' && method === 'POST') {
        return { deleted: { applications: 2, submissions: 1, runs: 3 } }
      }
      return null
    })

    render(<SetupPage />)

    await screen.findByText('Workspace Readiness')
    await screen.findByText('Profile Mode')
    await screen.findByText('.fmj/local-overrides/filefirst/user-profile.yml')
    fireEvent.click(screen.getByRole('button', { name: 'Reset Operational Data' }))

    await waitFor(() => {
      expect(screen.getByText(/Reset complete\. Cleared 2 applications/)).toBeTruthy()
    })
    expect(calls.some((call) => call.url === '/api/workspace/reset-operational' && call.method === 'POST')).toBe(true)
    expect(calls.filter((call) => call.url === '/api/setup/readiness').length).toBeGreaterThan(1)
  })

  it('settings preserves dirty portal edits, shows launch scope, and saves toggles', async () => {
    let settingsPayload = createSettingsPayload()
    const { calls } = installFetchMock(async ({ url, method, body }) => {
      if (url === '/api/settings') return settingsPayload
      if (url.startsWith('/api/settings/models/available')) {
        return { models: [{ id: 'runtime-qwen', label: 'runtime-qwen' }], count: 1, source: 'lmstudio', note: '1 models available from lmstudio.' }
      }
      if (url === '/api/settings/portals' && method === 'PUT') {
        return { saved: true, autonomous: { production_sources: ['greenhouse', 'lever'] } }
      }
      if (body) {
        return { saved: true }
      }
      return null
    })

    render(<SettingsPage />)

    await screen.findByText('Configuration')
    expect(screen.getByDisplayValue('greenhouse')).toBeTruthy()

    const greenhouseCard = screen.getByText('Greenhouse').closest('article')
    const leverCard = screen.getByText('Lever').closest('article')
    const greenhouseBoards = within(greenhouseCard).getAllByRole('textbox')[0]
    const leverToggle = within(leverCard).getByRole('checkbox')

    fireEvent.change(greenhouseBoards, { target: { value: 'acme\nmanual-board' } })
    fireEvent.click(leverToggle)

    settingsPayload = createSettingsPayload({
      autonomous: {
        ...createSettingsPayload().autonomous,
        production_sources: ['greenhouse', 'lever'],
      },
      portals: {
        sources: {
          greenhouse: { enabled: true, boards: ['server-pushed'], seed_urls: [], seed_domains: [] },
          lever: { enabled: false, boards: ['plaid'], seed_urls: [], seed_domains: [] },
          ashby: { enabled: false, boards: ['notion'], seed_urls: [], seed_domains: [] },
        },
        tracked_companies: [],
      },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Ping Runtime' }))

    await waitFor(() => {
      expect(calls.some((call) => call.url === '/api/settings/models/ping' && call.method === 'POST')).toBe(true)
      expect(screen.getByDisplayValue('greenhouse, lever')).toBeTruthy()
      expect(within(greenhouseCard).getAllByRole('textbox')[0].value).toBe('acme\nmanual-board')
      expect(within(leverCard).getByRole('checkbox').checked).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: 'Save Portals' }))
    await waitFor(() => {
      expect(screen.getByText('Portal settings saved.')).toBeTruthy()
    })

    const saveCall = calls.find((call) => call.url === '/api/settings/portals' && call.method === 'PUT')
    expect(saveCall).toBeTruthy()
    expect(JSON.parse(saveCall.body)).toEqual({
      sources: {
        greenhouse: { enabled: true, boards: ['acme', 'manual-board'], seed_urls: [], seed_domains: [] },
        lever: { enabled: true, boards: ['plaid'], seed_urls: [], seed_domains: [] },
        ashby: { enabled: false, boards: ['notion'], seed_urls: [], seed_domains: [] },
      },
      tracked_companies: [],
    })
  })

  it('settings runtime ping hits the backend ping endpoint', async () => {
    const { calls } = installFetchMock(async ({ url, method }) => {
      if (url === '/api/settings') return createSettingsPayload()
      if (url.startsWith('/api/settings/models/available')) {
        return { models: [{ id: 'runtime-qwen', label: 'runtime-qwen' }], count: 1, source: 'lmstudio', note: '1 models available from lmstudio.' }
      }
      if (url === '/api/settings/models/ping' && method === 'POST') {
        return { ok: true, profile: 'runtime-model', model: 'runtime-qwen' }
      }
      return { saved: true }
    })

    render(<SettingsPage />)

    await screen.findByText('Configuration')
    fireEvent.click(screen.getByRole('button', { name: 'Ping Runtime' }))

    await waitFor(() => {
      expect(calls.some((call) => call.url === '/api/settings/models/ping' && call.method === 'POST')).toBe(true)
      expect(screen.getByText('Ping ok for runtime-model.')).toBeTruthy()
    })
  })

  it('autopilot actions hit the correct APIs and refresh queue state', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    const { calls } = installFetchMock(async ({ url, method, body }) => {
      if (url === '/api/autonomous/status') {
        return {
          enabled: true,
          submit_enabled: true,
          default_submit_mode: 'auto_submit',
          ready_to_apply_threshold: 5,
          queue_depth: 1,
          blocked_by_questions: 1,
          unresolved_prompts: 1,
        }
      }
      if (url === '/api/questions/queue') {
        return {
          items: [
            {
              application_id: '001',
              question_id: 'work-auth',
              question_type: 'boolean',
              company: 'Acme',
              title: 'Backend Engineer',
              prompt_text: 'Authorized to work?',
              required: true,
              widget_type: 'text',
              existing_answer: '',
            },
          ],
        }
      }
      if (url === '/api/jobs/table?limit=100') {
        return {
          items: [
            { job_id: 'job-100', company: 'Acme', role: 'Backend Engineer', status: 'Ready to Submit', source: 'greenhouse', application_id: '001' },
          ],
        }
      }
      if (url === '/api/discover' && method === 'POST') return { started: true }
      if (url === '/api/autonomous/run' && method === 'POST') return { started: true, run_id: 'auto-1' }
      if (url === '/api/workspace/reset-operational' && method === 'POST') return { deleted: { applications: 1, jobs: 1, runs: 1 } }
      if (url === '/api/questions/answer' && method === 'POST') return { question: { existing_answer: 'Yes' }, remaining_blockers: [] }
      if (url === '/api/review/action' && method === 'POST') return { blocked: false, application_id: JSON.parse(body).application_id }
      if (url === '/api/jobs/purge-rejected' && method === 'POST') return { purged: 0 }
      return null
    })

    render(<AutopilotPage operator={buildOperator()} live={live} />)

    await screen.findByText('Execution Workspace')
    fireEvent.click(screen.getByRole('button', { name: 'Discover Jobs' }))
    fireEvent.click(screen.getByRole('button', { name: 'Full Run' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset Operational Data' }))
    fireEvent.change(screen.getByPlaceholderText('Type answer'), { target: { value: 'Yes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Answer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(calls.some((call) => call.url === '/api/discover' && call.method === 'POST')).toBe(true)
      expect(calls.some((call) => call.url === '/api/autonomous/run' && call.method === 'POST')).toBe(true)
      expect(calls.some((call) => call.url === '/api/workspace/reset-operational' && call.method === 'POST')).toBe(true)
      expect(calls.some((call) => call.url === '/api/questions/answer' && call.method === 'POST')).toBe(true)
      expect(calls.filter((call) => call.url === '/api/review/action' && call.method === 'POST').length).toBeGreaterThanOrEqual(1)
    })
    const answerCall = calls.find((call) => call.url === '/api/questions/answer' && call.method === 'POST')
    expect(JSON.parse(answerCall.body).auto_retry).toBe(true)
    expect(live.refresh).toHaveBeenCalled()
  })

  it('review page honors deep links and falls forward when the selected application leaves the queue', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    let reviewQueue = {
      items: [
        { application_id: '001', company: 'Acme', title: 'Backend Engineer', status: 'needs_user_input', review_status: 'needs_user_input' },
        { application_id: '002', company: 'Bravo', title: 'Platform Engineer', status: 'ready_for_review', review_status: 'ready_for_review' },
      ],
    }
    const detailById = {
      '001': {
        application: { application_id: '001', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
        submission: { status: 'needs_user_input' },
        blockers: [{ category: 'missing_required_field', label: 'Need start date' }],
        questions: [{ question_id: 'q1', prompt_text: 'Need start date', existing_answer: '' }],
        report_markdown: '# Acme',
      },
      '002': {
        application: { application_id: '002', company: 'Bravo', role: 'Platform Engineer', status: 'Ready For Review', source: 'greenhouse' },
        submission: { status: 'preview_ready' },
        blockers: [],
        questions: [],
        report_markdown: '# Bravo',
      },
    }

    installFetchMock(async ({ url, method }) => {
      if (url === '/api/review/queue') return reviewQueue
      if (url.startsWith('/api/applications/')) return detailById[url.split('/').pop()]
      if (url === '/api/review/action' && method === 'POST') {
        reviewQueue = { items: [reviewQueue.items[0]] }
        return { blocked: false }
      }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=002')

    await screen.findByText('Selected Application')
    const detailSection = screen.getByText('Selected Application').closest('section')
    await waitFor(() => {
      expect(within(detailSection).getByRole('heading', { name: /Bravo/i })).toBeTruthy()
    })
    expect(window.location.search).toBe('?application_id=002')
    expect(screen.queryByText('Current Process')).toBeNull()
    expect(screen.queryByText(/Shortcuts:/)).toBeNull()
    expect(screen.queryByRole('tab')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Approve / Apply' }))

    await waitFor(() => {
      expect(window.location.search).toBe('?application_id=001')
      expect(within(detailSection).getByRole('heading', { name: /Acme/i })).toBeTruthy()
    })
    expect(live.refresh).toHaveBeenCalled()
  })

  it('review page can record manual submissions from the action bar', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    const calls = []
    let reviewQueue = {
      items: [
        { application_id: '001', company: 'Acme', title: 'Backend Engineer', status: 'needs_user_input', review_status: 'needs_user_input' },
      ],
    }

    installFetchMock(async ({ url, method, body }) => {
      calls.push({ url, method, body })
      if (url === '/api/review/queue') return reviewQueue
      if (url === '/api/applications/001') {
        return {
          application: { application_id: '001', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
          submission: { status: 'needs_user_input' },
          blockers: [{ category: 'missing_required_field', label: 'Certification' }],
          questions: [],
          report_markdown: '# Acme',
        }
      }
      if (url === '/api/review/action' && method === 'POST') {
        reviewQueue = { items: [] }
        return { body: { blocked: false, manual_submitted: true, status: 'submitted' } }
      }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=001')

    await screen.findByText('Selected Application')
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(screen.getByRole('button', { name: 'Mark As Submitted' }))
    fireEvent.change(screen.getByPlaceholderText('Optional note about how you submitted it manually'), { target: { value: 'Submitted manually from the ATS.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Manual Submission' }))

    await waitFor(() => {
      expect(screen.getByText('Marked this application as submitted. It now counts toward submitted totals and leaves the review queue.')).toBeTruthy()
    })

    const reviewCall = calls.find((call) => call.url === '/api/review/action' && call.method === 'POST')
    expect(JSON.parse(reviewCall.body).action).toBe('mark_submitted')
    expect(JSON.parse(reviewCall.body).reason).toBe('Submitted manually from the ATS.')
    expect(live.refresh).toHaveBeenCalled()
  })

  it('review page saves unresolved question answers without auto retry and request input reports the parked handoff', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    const calls = []
    installFetchMock(async ({ url, method, body }) => {
      calls.push({ url, method, body })
      if (url === '/api/review/queue') {
        return {
          items: [
            { application_id: '001', company: 'Acme', title: 'Backend Engineer', status: 'needs_user_input', review_status: 'needs_user_input' },
          ],
        }
      }
      if (url === '/api/applications/001') {
        return {
          application: { application_id: '001', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
          submission: { status: 'needs_user_input' },
          blockers: [{ category: 'missing_required_field', label: 'Certification' }],
          questions: [
            {
              question_id: 'q1',
              prompt_text: 'Certification',
              existing_answer: '',
              needs_user_input: true,
              widget_type: 'select',
              option_details: [{ label: 'Yes', value: 'Yes' }, { label: 'No', value: 'No' }],
            },
          ],
          report_markdown: '# Acme',
        }
      }
      if (url === '/api/questions/answer' && method === 'POST') return { question: { existing_answer: 'Yes' }, remaining_blockers: [] }
      if (url === '/api/review/action' && method === 'POST') return { blocked: true, manual_handoff_opened: true, remaining_blockers: [] }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=001')

    await screen.findByText('Selected Application')
    const questionCard = (await screen.findAllByText('Certification')).find((node) => node.closest('.question-card'))
    const questionPanel = questionCard.closest('.question-card')
    fireEvent.change(within(questionPanel).getByRole('combobox'), { target: { value: 'Yes' } })
    fireEvent.click(within(questionPanel).getByRole('button', { name: 'Save Answer' }))

    await waitFor(() => {
      expect(calls.some((call) => call.url === '/api/questions/answer' && call.method === 'POST')).toBe(true)
    })
    const answerCall = calls.find((call) => call.url === '/api/questions/answer' && call.method === 'POST')
    expect(JSON.parse(answerCall.body).auto_retry).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Open For Manual Input' }))

    await waitFor(() => {
      expect(screen.getByText('Opened the partially filled application for manual completion. No submit attempt was made.')).toBeTruthy()
    })
  })

  it('review page can sync browser changes from a parked manual handoff', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    const calls = []
    installFetchMock(async ({ url, method, body }) => {
      calls.push({ url, method, body })
      if (url === '/api/review/queue') {
        return {
          items: [
            { application_id: '001', company: 'Acme', title: 'Backend Engineer', status: 'needs_user_input', review_status: 'needs_user_input' },
          ],
        }
      }
      if (url === '/api/applications/001') {
        return {
          application: { application_id: '001', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
          submission: { status: 'needs_user_input', result: { manual_handoff_watch: { active: true, status: 'watching', synced_question_count: 0, filled_blank_count: 0, corrected_answer_count: 0, recent_answers: [] } } },
          manual_handoff_watch: { active: true, status: 'watching', synced_question_count: 0, filled_blank_count: 0, corrected_answer_count: 0, recent_answers: [] },
          blockers: [{ category: 'missing_required_field', label: 'Certification' }],
          questions: [],
          report_markdown: '# Acme',
        }
      }
      if (url === '/api/review/action' && method === 'POST') {
        const payload = JSON.parse(body)
        if (payload.action === 'sync_manual_input') {
          return { page_found: true, synced_count: 2, filled_blank_count: 1, corrected_answer_count: 1, blocked: false, remaining_blockers: [] }
        }
        return { blocked: false }
      }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=001')

    await screen.findByText('Selected Application')
    fireEvent.click(screen.getByRole('button', { name: 'Sync Browser Changes' }))

    await waitFor(() => {
      expect(screen.getByText('Synced 2 browser answers. 1 filled blank field, 1 corrected existing answer.')).toBeTruthy()
    })

    const syncCall = calls.find((call) => call.url === '/api/review/action' && call.method === 'POST' && JSON.parse(call.body).action === 'sync_manual_input')
    expect(syncCall).toBeTruthy()
    expect(live.refresh).toHaveBeenCalled()
  })

  it('review page serializes checkbox-group answers for reuse', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    const calls = []
    installFetchMock(async ({ url, method, body }) => {
      calls.push({ url, method, body })
      if (url === '/api/review/queue') {
        return {
          items: [
            { application_id: '003', company: 'Acme', title: 'Backend Engineer', status: 'needs_user_input', review_status: 'needs_user_input' },
          ],
        }
      }
      if (url === '/api/applications/003') {
        return {
          application: { application_id: '003', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
          submission: { status: 'needs_user_input' },
          blockers: [{ category: 'missing_required_field', label: 'Preferred work locations' }],
          questions: [
            {
              question_id: 'q-locations',
              prompt_text: 'Preferred work locations',
              existing_answer: '',
              needs_user_input: true,
              widget_type: 'checkbox_group',
              option_details: [{ label: 'Remote', value: 'remote' }, { label: 'New York', value: 'nyc' }],
            },
          ],
          report_markdown: '# Acme',
        }
      }
      if (url === '/api/questions/answer' && method === 'POST') return { question: { existing_answer: 'Remote, New York' }, remaining_blockers: [] }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=003')

    await screen.findByText('Selected Application')
    const locationsCard = (await screen.findAllByText('Preferred work locations')).find((node) => node.closest('.question-card'))
    const questionPanel = locationsCard.closest('.question-card')
    fireEvent.click(within(questionPanel).getByLabelText('Remote'))
    fireEvent.click(within(questionPanel).getByLabelText('New York'))
    fireEvent.click(within(questionPanel).getByRole('button', { name: 'Save Answer' }))

    await waitFor(() => {
      expect(calls.some((call) => call.url === '/api/questions/answer' && call.method === 'POST')).toBe(true)
    })
    const answerCall = calls.find((call) => call.url === '/api/questions/answer' && call.method === 'POST')
    expect(JSON.parse(answerCall.body).answer_text).toBe('Remote, New York')
  })

  it('review page honors legacy tab query params and opens the matching section', async () => {
    const live = { refresh: vi.fn().mockResolvedValue({}) }
    installFetchMock(async ({ url }) => {
      if (url === '/api/review/queue') {
        return {
          items: [
            {
              application_id: '001',
              company: 'Acme',
              title: 'Backend Engineer',
              status: 'needs_user_input',
              review_status: 'needs_user_input',
              review_summary: { severity: 'danger', next_action: 'save_answers', next_action_reason: 'Needs answers.' },
              manual_handoff: { active: false, status: 'idle', pending_count: 0 },
            },
          ],
        }
      }
      if (url === '/api/applications/001') {
        return {
          application: { application_id: '001', company: 'Acme', role: 'Backend Engineer', status: 'Needs Input', source: 'greenhouse' },
          submission: { status: 'needs_user_input' },
          summary: { next_action: 'save_answers', next_action_reason: 'Needs answers.', blocker_count: 1, warning_count: 0, missing_required_count: 1, ungrounded_count: 0, low_confidence_count: 0, classification: {} },
          blockers: [{ category: 'missing_required_field', label: 'Need start date' }],
          questions: [],
          artifacts: [],
          history: [{ type: 'review.answer.saved', actor: 'operator', summary: 'Saved an answer.', timestamp: '2026-04-15T12:00:00Z', metadata: {} }],
          report_markdown: '# Acme',
        }
      }
      return null
    })

    renderWithRouter(<ReviewPage operator={buildOperator()} live={live} />, '/review?application_id=001&tab=history')

    await screen.findByText('Selected Application')
    expect(screen.getByText('Saved an answer.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Questions/i }))

    await waitFor(() => {
      expect(window.location.search).toContain('tab=questions')
    })
  })

  it('app exposes four primary nav items and keeps /runs on the dashboard view', async () => {
    installFetchMock(async ({ url }) => {
      if (url === '/api/live/status?limit=60') {
        return { state: { run_type: 'idle', status: 'idle', stage: 'idle', latest_operator_message: 'No active run.' }, events: [] }
      }
      if (url === '/api/dashboard') {
        return {
          snapshot: { counts: { inbox: 1, applications: 1 } },
          autonomous: { queue_depth: 1, blocked_by_questions: 0, ready_to_apply: 1, ready_to_apply_threshold: 5, unresolved_prompts: 0 },
          jobs_table: { items: [] },
        }
      }
      if (url === '/api/runs/history') {
        return {
          items: [
            {
              run_id: 'run-001',
              run_type: 'autonomous',
              status: 'completed',
              started_at: '2026-04-13T12:00:00Z',
              completed_at: '2026-04-13T12:15:00Z',
              submitted_count: 2,
              failed_count: 1,
              processed_count: 8,
              evaluated_count: 5,
            },
          ],
        }
      }
      return null
    })

    renderWithRouter(<App />, '/runs')

    await screen.findByText('Overview And Health')
    expect(screen.queryByText('Live Operator State')).toBeNull()
    expect(screen.getByRole('link', { name: 'Dashboard' }).className).toContain('active')
    expect(screen.getByRole('link', { name: 'Autopilot' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Review' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Settings' })).toBeTruthy()
    expect(screen.getByText('run-001')).toBeTruthy()
    expect(screen.getByText(/submitted 2 \/ failed 1/)).toBeTruthy()
  })
})
