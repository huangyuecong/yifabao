/** A synchronous batch includes sequential provider calls and result downloads. */
export function imageBatchTimeout(count = 1) {
  const boundedCount = Number.isFinite(count) ? Math.min(8, Math.max(1, Math.trunc(count))) : 1
  return boundedCount * 300_000 + 60_000
}

/** Share an unfinished request across editor remounts; never replay a failed POST. */
export function createImageBatchRequests<T>() {
  const pending = new Map<string, { signature: string, promise: Promise<T> }>()
  return (projectId: string, parameters: unknown, send: () => Promise<T>): Promise<T> => {
    const signature = JSON.stringify(parameters)
    const existing = pending.get(projectId)
    if (existing) {
      return existing.signature === signature
        ? existing.promise
        : Promise.reject(new Error('此工程已有图片正在生成，请等待完成后再提交新的要求'))
    }
    const promise = Promise.resolve().then(send).finally(() => {
      if (pending.get(projectId)?.promise === promise) {
        pending.delete(projectId)
      }
    })
    pending.set(projectId, { signature, promise })
    return promise
  }
}
