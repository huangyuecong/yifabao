export interface ImageGenerationDraft {
  count?: number
  image_prompt: string
  image_scene: string
  image_style: string
  image_ratio: string
  image_size: string
  reference_artifact_ids: string[]
  reference_asset_ids: string[]
  knowledge_resource_ids: string[]
}

export interface ImageGenerationDraftSource {
  parameters?: Record<string, unknown>
  requested_count?: number
  source_artifact_id?: string | null
  created_at?: string
}

export interface ImageArtifactDraftSource {
  kind: string
  content: Record<string, unknown>
  created_at?: string
  version?: number
}

export type RecoveredImageGenerationDraft = Partial<ImageGenerationDraft>

const hasOwn = (value: Record<string, unknown>, key: string) =>
  Object.hasOwn(value, key)

function stringValue(value: unknown) {
  return value == null ? '' : String(value)
}

function stringArray(value: unknown) {
  return Array.isArray(value)
    ? [...new Set(value.map(item => String(item).trim()).filter(Boolean))]
    : []
}

function newestFirst<T extends { created_at?: string, version?: number }>(values: readonly T[]) {
  return values
    .map((value, index) => ({ value, index }))
    .sort((left, right) => {
      const versionDifference = Number(right.value.version || 0) - Number(left.value.version || 0)
      return versionDifference || left.index - right.index
    })
    .map(item => item.value)
}

const fieldParameters: Array<{
  draftKey: keyof ImageGenerationDraft
  parameterKey: string
  array: boolean
}> = [
  { draftKey: 'image_prompt', parameterKey: 'prompt', array: false },
  { draftKey: 'image_scene', parameterKey: 'scene', array: false },
  { draftKey: 'image_style', parameterKey: 'style', array: false },
  { draftKey: 'image_ratio', parameterKey: 'ratio', array: false },
  { draftKey: 'image_size', parameterKey: 'size', array: false },
]

function formState(value: Record<string, unknown>) {
  const candidate = value.form_state
  return candidate && typeof candidate === 'object' && !Array.isArray(candidate)
    ? candidate as Record<string, unknown>
    : undefined
}

function isImageRevisionRun(run: ImageGenerationDraftSource) {
  const parameters = run.parameters ?? {}
  const sourceArtifactId = String(
    run.source_artifact_id || parameters.source_artifact_id || '',
  ).trim()
  const referenceArtifactIds = Array.isArray(parameters.reference_artifact_ids)
    ? parameters.reference_artifact_ids
    : []
  return Boolean(sourceArtifactId)
    || (run.requested_count === 1 && referenceArtifactIds.length > 0)
}

export function latestGenerationRequestedCount(runs: readonly ImageGenerationDraftSource[]) {
  const count = runs[0]?.requested_count
  return Number.isInteger(count) && Number(count) > 0 ? Number(count) : undefined
}

function fieldFromSource(
  source: Record<string, unknown>,
  draftKey: keyof ImageGenerationDraft,
  parameterKey: string,
) {
  const snapshot = formState(source)
  if (snapshot && hasOwn(snapshot, draftKey)) {
    return { found: true, value: snapshot[draftKey] }
  }
  if (hasOwn(source, parameterKey)) {
    return { found: true, value: source[parameterKey] }
  }
  return { found: false, value: undefined }
}

/**
 * Restore each field independently. Image revisions often omit scene/style, so
 * a missing field falls back to an older successful run. An explicitly stored
 * empty value in `form_state` remains empty and does not resurrect old input.
 * Reference IDs are intentionally excluded: current task/product configuration
 * is authoritative and old runs must never silently bring prior brand assets
 * back into a new generation.
 */
export function recoverImageGenerationDraft(
  runs: readonly ImageGenerationDraftSource[],
  artifacts: readonly ImageArtifactDraftSource[] = [],
): RecoveredImageGenerationDraft {
  const orderedRuns = newestFirst(runs)
  const orderedArtifacts = newestFirst(artifacts.filter(item => item.kind === 'image'))
  const recovered: RecoveredImageGenerationDraft = {}
  const snapshotCount = orderedRuns
    .map(run => formState(run.parameters ?? {})?.count)
    .find(value => Number.isInteger(value) && Number(value) > 0)
  const fallbackCount = latestGenerationRequestedCount([
    orderedRuns.find(run => !isImageRevisionRun(run))
    ?? orderedRuns[0]
    ?? {},
  ])
  if (snapshotCount != null) {
    recovered.count = Number(snapshotCount)
  }
  else if (fallbackCount != null) {
    recovered.count = fallbackCount
  }

  for (const field of fieldParameters) {
    const sources = [
      ...orderedRuns.map(run => run.parameters ?? {}),
      ...orderedArtifacts.map(artifact => artifact.content),
    ]
    for (const source of sources) {
      const candidate = fieldFromSource(source, field.draftKey, field.parameterKey)
      if (!candidate.found) {
        continue
      }
      Object.assign(recovered, {
        [field.draftKey]: field.array
          ? stringArray(candidate.value)
          : stringValue(candidate.value),
      })
      break
    }
  }
  return recovered
}

export function imageGenerationFormState(value: ImageGenerationDraft): ImageGenerationDraft {
  return {
    count: Number.isInteger(value.count) && Number(value.count) > 0
      ? Number(value.count)
      : undefined,
    image_prompt: value.image_prompt,
    image_scene: value.image_scene,
    image_style: value.image_style,
    image_ratio: value.image_ratio,
    image_size: value.image_size,
    reference_artifact_ids: [...value.reference_artifact_ids],
    reference_asset_ids: [...value.reference_asset_ids],
    knowledge_resource_ids: [...value.knowledge_resource_ids],
  }
}
