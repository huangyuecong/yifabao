import type { MediaJobResponse } from '../../../../api/agent/content'

export function isTerminalMediaJob(job: Pick<MediaJobResponse, 'status'>): boolean {
  return job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled'
}

/** Replace an existing job in-place while retaining newest-first list ordering. */
export function upsertMediaJob(
  jobs: readonly MediaJobResponse[],
  next: MediaJobResponse,
): MediaJobResponse[] {
  const index = jobs.findIndex(job => job.id === next.id)
  if (index < 0) {
    return [next, ...jobs]
  }
  return jobs.map((job, jobIndex) => jobIndex === index ? next : job)
}

/** Keep the selected frame rate for both precise previews and final renders. */
export function previewFps(canvasFps: number): number {
  return Number.isFinite(canvasFps) ? Math.min(60, Math.max(1, Math.round(canvasFps))) : 25
}
