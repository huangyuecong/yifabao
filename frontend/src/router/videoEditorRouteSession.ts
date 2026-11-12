interface VideoEditorHandoff {
  from: string
  to: string
  taskViewKey: string
}

let handoff: VideoEditorHandoff | undefined

/** One keep-alive instance for the whole content agent, including query-string changes. */
export const CONTENT_AGENT_SHELL_KEY = 'content-agent-workbench'

function routePath(fullPath: string) {
  return fullPath.split(/[?#]/u)[0]
}

function isContentAgentRoute(fullPath: string) {
  const path = routePath(fullPath)
  return path === '/agent/content' || path.startsWith('/agent/content/')
}

/** Preserve just the current new-video editor while its first saved URL is adopted. */
export function beginVideoEditorRouteHandoff(from: string, taskId: string, taskViewKey: string) {
  if (from.split(/[?#]/u)[0] !== '/agent/content/create/video' || !taskId) {
    return false
  }
  handoff = {
    from,
    to: `/agent/content/tasks/${encodeURIComponent(taskId)}/stages/video_preview`,
    taskViewKey,
  }
  return true
}

function currentHandoff(fullPath: string) {
  if (handoff && fullPath !== handoff.from && fullPath !== handoff.to) {
    handoff = undefined
  }
  return handoff
}

export function contentRoutePageKey(route: { fullPath: string, meta: Record<string, unknown> }) {
  if (route.meta.appType === 'micro-app') {
    handoff = undefined
    return route.meta.origin as string | undefined
  }
  currentHandoff(route.fullPath)
  if (isContentAgentRoute(route.fullPath)) {
    return CONTENT_AGENT_SHELL_KEY
  }
  return route.fullPath
}

export function contentCreateViewKey(mode: string, queryNew: unknown) {
  const value = Array.isArray(queryNew) ? queryNew[0] : queryNew
  return `create:${mode}:${String(value || '')}`
}

export function contentTaskViewKey(fullPath: string, defaultKey: string) {
  return currentHandoff(fullPath)?.taskViewKey ?? defaultKey
}
