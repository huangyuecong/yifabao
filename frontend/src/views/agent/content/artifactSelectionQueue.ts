export interface LatestSelectionQueue<T> {
  enqueue: (value: T) => void
  flush: () => Promise<boolean>
  clear: () => void
}

interface LatestSelectionQueueOptions {
  onActiveChange?: (active: boolean) => void
  onError?: (error: unknown) => void | Promise<void>
}

/**
 * Persist the first selection immediately while coalescing rapid follow-up
 * changes to the latest value. Writes are strictly serial so every request can
 * use the stage version returned by the previous request.
 */
export function createLatestSelectionQueue<T>(
  persist: (value: T) => Promise<void>,
  options: LatestSelectionQueueOptions = {},
): LatestSelectionQueue<T> {
  let pending: T | undefined
  let active: Promise<void> | undefined
  let failureCount = 0
  let recoveringFailure = false

  const start = () => {
    if (active || pending === undefined) {
      return
    }
    active = (async () => {
      options.onActiveChange?.(true)
      try {
        while (pending !== undefined) {
          const value = pending
          pending = undefined
          await persist(value)
        }
      }
      catch (error) {
        pending = undefined
        failureCount += 1
        recoveringFailure = true
        await options.onError?.(error)
      }
      finally {
        recoveringFailure = false
        options.onActiveChange?.(false)
        active = undefined
        if (pending !== undefined) {
          start()
        }
      }
    })()
    // Enqueues are intentionally fire-and-forget. `flush` remains the explicit
    // synchronization point used before confirm/navigation.
    void active.catch(() => undefined)
  }

  return {
    enqueue(value) {
      pending = value
      start()
    },
    async flush() {
      const initialFailureCount = failureCount
      const failureWasRecovering = recoveringFailure
      start()
      for (;;) {
        const running = active
        if (!running) {
          break
        }
        await running
        start()
      }
      return !failureWasRecovering
        && failureCount === initialFailureCount
        && pending === undefined
    },
    clear() {
      pending = undefined
    },
  }
}

export interface SelectableArtifact {
  id: string
  kind: string
  content?: Record<string, unknown>
}

export function nextArtifactSelection(
  selectedIds: readonly string[],
  artifact: SelectableArtifact,
  stageKey: string,
  artifacts: readonly SelectableArtifact[],
) {
  if (stageKey === 'voice_subtitles') {
    const byId = new Map(artifacts.map(item => [item.id, item]))
    if (selectedIds.includes(artifact.id)) {
      return selectedIds.filter((id) => {
        if (id === artifact.id) {
          return false
        }
        const selected = byId.get(id)
        return artifact.kind !== 'voice'
          || selected?.kind !== 'subtitle'
          || selected.content?.source_artifact_id !== artifact.id
      })
    }
    if (artifact.kind === 'voice') {
      return [
        ...selectedIds.filter((id) => {
          const selected = byId.get(id)
          return selected?.kind !== 'voice'
            && (selected?.kind !== 'subtitle'
              || selected.content?.source_artifact_id === artifact.id)
        }),
        artifact.id,
      ]
    }
    if (artifact.kind === 'subtitle') {
      const sourceVoiceId = String(artifact.content?.source_artifact_id || '').trim()
      const sourceVoice = artifacts.find(item => item.id === sourceVoiceId && item.kind === 'voice')
      if (!sourceVoice) {
        return [...selectedIds]
      }
      return [
        ...selectedIds.filter((id) => {
          const selected = byId.get(id)
          return selected?.kind !== 'voice' && selected?.kind !== 'subtitle'
        }),
        sourceVoice.id,
        artifact.id,
      ]
    }
  }
  // Copy always has one authoritative "current" version. Clicking the
  // already-current card is therefore a no-op; users switch the current copy
  // by choosing another version instead of temporarily clearing the lineage.
  if (stageKey === 'copy') {
    return selectedIds.includes(artifact.id) ? [...selectedIds] : [artifact.id]
  }
  if (selectedIds.includes(artifact.id)) {
    return selectedIds.filter(id => id !== artifact.id)
  }
  if (['topic', 'compliance', 'storyboard'].includes(stageKey)) {
    return [artifact.id]
  }
  return [...selectedIds, artifact.id]
}
