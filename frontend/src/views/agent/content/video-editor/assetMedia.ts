import type { VideoProjectAsset } from '@/api/agent/content'

export type ProjectAssetMediaKind = 'image' | 'video' | 'audio' | 'file'

/** Resolve a media duration from every metadata shape emitted by ingest/generation. */
export function mediaMetadataDurationSeconds(
  metadata: Record<string, unknown> | null | undefined,
): number | undefined {
  const valueMetadata = metadata || {}
  const probe = valueMetadata.probe
  const probeDuration
    = probe && typeof probe === 'object' && !Array.isArray(probe)
      ? (probe as Record<string, unknown>).duration_seconds
      : undefined
  const provenance = valueMetadata.provenance
  const provenanceRequest
    = provenance && typeof provenance === 'object' && !Array.isArray(provenance)
      ? (provenance as Record<string, unknown>).request
      : undefined
  const provenanceDuration
    = provenanceRequest && typeof provenanceRequest === 'object' && !Array.isArray(provenanceRequest)
      ? (provenanceRequest as Record<string, unknown>).duration_seconds
      : undefined
  for (const candidate of [valueMetadata.duration_seconds, probeDuration, provenanceDuration]) {
    const value = Number(candidate)
    if (Number.isFinite(value) && value > 0) {
      return value
    }
  }
  return undefined
}

export function assetMediaKind(
  asset: Pick<VideoProjectAsset, 'metadata' | 'role'>,
): ProjectAssetMediaKind {
  const mediaKind = String(asset.metadata.media_kind || '')
  const mediaType = String(asset.metadata.media_type || '')
  if (asset.role === 'voice' || asset.role === 'music' || asset.role === 'sfx') {
    return 'audio'
  }
  if (mediaKind === 'image' || mediaType.startsWith('image/')) {
    return 'image'
  }
  if (mediaKind === 'audio' || mediaType.startsWith('audio/')) {
    return 'audio'
  }
  if (mediaKind === 'video' || mediaType.startsWith('video/')) {
    return 'video'
  }
  return 'file'
}

export function projectAssetDurationSeconds(
  asset: Pick<VideoProjectAsset, 'metadata'>,
): number | undefined {
  return mediaMetadataDurationSeconds(asset.metadata)
}

export function isProviderNativeVideoReference(
  asset: Pick<VideoProjectAsset, 'metadata' | 'source_type'>,
  supported: boolean,
  currentProviderId: string | undefined,
): boolean {
  if (!supported || asset.source_type !== 'artifact' || asset.metadata.source !== 'ai-generated') {
    return false
  }
  const reference = asset.metadata.provider_native_reference
  if (!reference || typeof reference !== 'object' || Array.isArray(reference)) {
    return false
  }
  const providerId = String(reference.provider_id || '').trim()
  return (
    reference.eligible === true
    && Boolean(providerId)
    && providerId === String(currentProviderId || '').trim()
    && Boolean(String(reference.transport || '').trim())
    && Boolean(String(reference.reference_kind || '').trim())
  )
}

/**
 * Select a playable/visible source without confusing waveform and thumbnail
 * derivatives with the original media after a project reload.
 */
export function selectProjectAssetPreviewUrl(asset: VideoProjectAsset): string {
  const kind = assetMediaKind(asset)
  if (kind === 'audio') {
    return asset.stream_url || asset.original_url || ''
  }
  if (kind === 'video') {
    const proxy = asset.derivatives.find(
      derivative =>
        derivative.kind === 'proxy'
        && (!derivative.media_type || derivative.media_type.startsWith('video/')),
    )
    return proxy?.download_url || asset.stream_url || asset.original_url || ''
  }
  if (kind === 'image') {
    const thumbnail = asset.derivatives.find(
      derivative => derivative.kind === 'thumbnail' || derivative.media_type?.startsWith('image/'),
    )
    return thumbnail?.download_url || asset.original_url || asset.stream_url || ''
  }
  const safeDerivative = asset.derivatives.find(derivative => derivative.kind !== 'waveform')
  return asset.original_url || asset.stream_url || safeDerivative?.download_url || ''
}
