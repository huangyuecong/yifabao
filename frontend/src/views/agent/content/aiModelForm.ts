import type { ModelConfiguration, ModelConfigurationPayload } from '@/api/agent/aiModels'

export function modelFormValues(configuration: Pick<ModelConfiguration, 'values'> & Partial<Pick<ModelConfiguration, 'adapters' | 'configured_secrets'>>): Record<string, string> {
  const secretFields = new Set([
    ...(configuration.configured_secrets ?? []),
    ...(configuration.adapters ?? []).flatMap(adapter => adapter.fields.filter(field => field.kind === 'secret').map(field => field.key)),
  ])
  return Object.fromEntries(Object.entries(configuration.values).filter(([key]) => !secretFields.has(key)).map(([key, value]) => [
    key,
    Array.isArray(value) ? value.join(', ') : value ?? '',
  ]))
}

export function canKeepModelSecret(configuration: ModelConfiguration | undefined, provider: string, values: Record<string, string>, key: string) {
  if (!configuration || configuration.provider !== provider || !configuration.configured_secrets.includes(key)) {
    return false
  }
  const adapter = configuration.adapters.find(item => item.value === provider)
  if (!adapter) {
    return false
  }
  return adapter.fields.filter(field => field.kind === 'url').every(field =>
    (values[field.key] || '').trim() === String(configuration.values[field.key] || '').trim(),
  )
}

export function modelFormPayload(
  configuration: ModelConfiguration,
  provider: string,
  values: Record<string, string>,
  revision: number,
): ModelConfigurationPayload {
  const fields = configuration.adapters.find(item => item.value === provider)?.fields ?? []
  return {
    provider,
    expected_revision: revision,
    values: Object.fromEntries(fields.filter(field => field.kind !== 'secret' || Boolean(values[field.key]?.trim())).map(field => [field.key, values[field.key] ?? ''])),
  }
}

export function modelFieldKey(capability: string) {
  return ({ text: 'llm_model', image: 'image_model', video: 'ai_video_model', speech: 'speech_resource_id', music: 'music_model' } as Record<string, string>)[capability]
}
