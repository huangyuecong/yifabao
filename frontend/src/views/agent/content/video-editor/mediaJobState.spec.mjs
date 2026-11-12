import { describe, expect, it } from 'vitest'
import { isTerminalMediaJob, previewFps, upsertMediaJob } from './mediaJobState.ts'

const job = (id, status = 'queued', progress = 0) => ({
  id,
  task_id: 'task-1',
  kind: 'video_project_render',
  status,
  progress,
  result: {},
  attempt: 0,
  max_attempts: 3,
  created_at: '2026-08-28T00:00:00Z',
  updated_at: '2026-08-28T00:00:00Z',
})

describe('media job state helpers', () => {
  it('identifies only terminal job statuses', () => {
    expect(isTerminalMediaJob(job('a', 'queued'))).toBe(false)
    expect(isTerminalMediaJob(job('a', 'running'))).toBe(false)
    expect(isTerminalMediaJob(job('a', 'succeeded'))).toBe(true)
    expect(isTerminalMediaJob(job('a', 'failed'))).toBe(true)
    expect(isTerminalMediaJob(job('a', 'cancelled'))).toBe(true)
  })

  it('upserts without changing newest-first ordering', () => {
    const first = job('a', 'running', 20)
    const second = job('b')
    expect(upsertMediaJob([first, second], job('a', 'succeeded', 100))).toEqual([
      job('a', 'succeeded', 100),
      second,
    ])
    expect(upsertMediaJob([first], second).map(item => item.id)).toEqual(['b', 'a'])
  })

  it('normalizes unsupported canvas frame rates for preview', () => {
    expect(previewFps(24)).toBe(25)
    expect(previewFps(25)).toBe(25)
    expect(previewFps(30)).toBe(30)
    expect(previewFps(60)).toBe(30)
    expect(previewFps(Number.NaN)).toBe(30)
  })
})
