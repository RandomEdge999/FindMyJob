import { describe, expect, it } from 'vitest'

import { deriveOperatorState } from './app.jsx'


describe('deriveOperatorState', () => {
  it('treats recent heartbeats as fresh activity during long-running work', () => {
    const staleEventAt = new Date(Date.now() - 30_000).toISOString()
    const recentHeartbeatAt = Date.now() - 5_000

    const operator = deriveOperatorState(
      {
        state: {
          run_type: 'autonomous',
          status: 'running',
          stage: 'drafting',
          last_event_at: staleEventAt,
          stats: {},
        },
        events: [],
      },
      'connected',
      recentHeartbeatAt,
    )

    expect(operator.streamHealth).toBe('connected')
    expect(operator.warningNotice).toBe('')
  })

  it('handles missing payload shape without crashing', () => {
    const operator = deriveOperatorState(null, 'connecting', Date.now())

    expect(operator.runType).toBe('idle')
    expect(operator.status).toBe('idle')
    expect(operator.events).toEqual([])
  })

  it('surfaces queued ChatGPT drafting statuses through counters and labels', () => {
    const operator = deriveOperatorState(
      {
        state: {
          run_type: 'autonomous',
          status: 'running',
          stage: 'drafting',
          latest_operator_message: 'awaiting_markers',
          stats: {
            drafted: 2,
            ready_for_submit: 1,
          },
        },
        events: [],
      },
      'connected',
      Date.now(),
    )

    expect(operator.stage).toBe('drafting')
    expect(operator.counters.drafted).toBe(2)
    expect(operator.counters.readyToApply).toBe(1)
    expect(operator.latestMessage).toBe('awaiting_markers')
  })

  it('suppresses recovered worker errors after a completed run', () => {
    const operator = deriveOperatorState(
      {
        state: {
          run_type: 'submission',
          status: 'completed',
          stage: 'submit',
          latest_operator_message: 'Submission finished successfully.',
          latest_error: 'Stale live state recovered without an active worker.',
          stats: {},
        },
        events: [],
      },
      'connected',
      Date.now(),
    )

    expect(operator.latestError).toBe('')
  })
})
