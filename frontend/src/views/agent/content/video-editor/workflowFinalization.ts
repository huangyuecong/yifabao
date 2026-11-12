export interface WorkflowStageSelectionState {
  version: number
  selected_artifact_ids?: Record<string, string[]>
  confirmed_artifact_ids?: Record<string, string[]>
}

export interface WorkflowStageBaseline {
  version: number
  selectedArtifactIds: string[]
  confirmedArtifactIds: string[]
}

export type WorkflowFinalizationDecision
  = | 'already_confirmed'
    | 'confirm_selected'
    | 'select_and_confirm'
    | 'conflict'

export function stableWorkflowArtifactIds(value?: Record<string, string[]>) {
  return [
    ...new Set(
      Object.values(value ?? {})
        .flat()
        .map(id => String(id || '').trim())
        .filter(Boolean),
    ),
  ].sort()
}

export function workflowStageBaseline(
  stage: WorkflowStageSelectionState,
): WorkflowStageBaseline {
  return {
    version: stage.version,
    selectedArtifactIds: stableWorkflowArtifactIds(stage.selected_artifact_ids),
    confirmedArtifactIds: stableWorkflowArtifactIds(stage.confirmed_artifact_ids),
  }
}

function sameIds(left: string[], right: string[]) {
  return left.length === right.length && left.every((id, index) => id === right[index])
}

export function isOnlyWorkflowArtifact(ids: string[], artifactId: string) {
  return ids.length === 1 && ids[0] === artifactId
}

export function workflowStageMatchesBaseline(
  stage: WorkflowStageSelectionState,
  baseline: WorkflowStageBaseline,
) {
  const current = workflowStageBaseline(stage)
  return current.version === baseline.version
    && sameIds(current.selectedArtifactIds, baseline.selectedArtifactIds)
    && sameIds(current.confirmedArtifactIds, baseline.confirmedArtifactIds)
}

export function workflowFinalizationDecision(
  stage: WorkflowStageSelectionState,
  baseline: WorkflowStageBaseline,
  finalArtifactId: string,
): WorkflowFinalizationDecision {
  const current = workflowStageBaseline(stage)
  if (isOnlyWorkflowArtifact(current.confirmedArtifactIds, finalArtifactId)) {
    return 'already_confirmed'
  }
  if (current.confirmedArtifactIds.includes(finalArtifactId)) {
    return 'conflict'
  }
  if (!workflowStageMatchesBaseline(stage, baseline)) {
    return 'conflict'
  }
  if (
    current.confirmedArtifactIds.length === 0
    && isOnlyWorkflowArtifact(current.selectedArtifactIds, finalArtifactId)
  ) {
    return 'confirm_selected'
  }
  return 'select_and_confirm'
}

export function finalConfirmationRetryDecision(
  stage: WorkflowStageSelectionState,
  finalArtifactId: string,
): Extract<WorkflowFinalizationDecision, 'already_confirmed' | 'confirm_selected' | 'conflict'> {
  const current = workflowStageBaseline(stage)
  if (isOnlyWorkflowArtifact(current.confirmedArtifactIds, finalArtifactId)) {
    return 'already_confirmed'
  }
  if (
    current.confirmedArtifactIds.length === 0
    && isOnlyWorkflowArtifact(current.selectedArtifactIds, finalArtifactId)
  ) {
    return 'confirm_selected'
  }
  return 'conflict'
}
