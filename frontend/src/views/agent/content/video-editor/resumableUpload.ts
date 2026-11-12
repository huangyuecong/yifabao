/* eslint-disable financial/no-division, financial/no-float-calculation */
import type {
  VideoProjectAsset,
  VideoProjectAssetRole,
  VideoUploadSessionResponse,
} from '@/api/agent/content'
import { contentApi } from '@/api/agent/content'
import { sha256Blob, sha256Chunk } from './sha256'

export const RESUMABLE_UPLOAD_THRESHOLD_BYTES = 32 * 1024 * 1024
export const RESUMABLE_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
const CHECKPOINT_PREFIX = 'yifabao-video-upload:'

export interface ProjectAssetUploadProgress {
  phase: 'hashing' | 'creating' | 'uploading' | 'finalizing'
  fraction: number
  message: string
}

export interface ProjectAssetUploadOptions {
  projectId: string
  file: File
  role: VideoProjectAssetRole
  signal?: AbortSignal
  onProgress?: (progress: ProjectAssetUploadProgress) => void
  onSession?: (session: VideoUploadSessionResponse, checkpointKey: string) => void
}

function abortError() {
  return new DOMException('上传已取消', 'AbortError')
}

function ensureNotAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw abortError()
  }
}

function checkpointKey(options: Pick<ProjectAssetUploadOptions, 'projectId' | 'file' | 'role'>) {
  const identity = [
    options.projectId,
    options.role,
    options.file.name,
    options.file.size,
    options.file.lastModified,
  ].map(encodeURIComponent).join(':')
  return `${CHECKPOINT_PREFIX}${identity}`
}

function readCheckpoint(key: string) {
  try {
    return localStorage.getItem(key) || ''
  }
  catch {
    return ''
  }
}

export function clearUploadCheckpoint(key: string) {
  try {
    localStorage.removeItem(key)
  }
  catch {
    // Private browsing can deny storage. Uploading remains functional without resume metadata.
  }
}

function writeCheckpoint(key: string, uploadId: string) {
  try {
    localStorage.setItem(key, uploadId)
  }
  catch {
    // The server session still works for this page even when persistence is unavailable.
  }
}

function matchesUpload(
  session: VideoUploadSessionResponse,
  options: ProjectAssetUploadOptions,
  checksum: string,
) {
  return session.project_id === options.projectId
    && session.role === options.role
    && session.filename === options.file.name
    && session.total_size === options.file.size
    && session.checksum_sha256.toLowerCase() === checksum.toLowerCase()
}

async function existingAsset(session: VideoUploadSessionResponse) {
  if (!session.project_asset_id) {
    return undefined
  }
  const project = await contentApi.getVideoProject(session.project_id)
  return project.assets.find(asset => asset.id === session.project_asset_id)
}

async function resolveUploadSession(
  options: ProjectAssetUploadOptions,
  checksum: string,
  key: string,
) {
  const storedId = readCheckpoint(key)
  if (storedId) {
    try {
      const existing = await contentApi.getVideoUpload(storedId)
      if (matchesUpload(existing, options, checksum)) {
        if (existing.status === 'uploading' || existing.status === 'completed') {
          return existing
        }
      }
    }
    catch {
      // Expired or inaccessible sessions are replaced with a new server session.
    }
    clearUploadCheckpoint(key)
  }
  options.onProgress?.({ phase: 'creating', fraction: 0.15, message: '正在创建可恢复上传会话' })
  const created = await contentApi.createVideoUpload({
    project_id: options.projectId,
    role: options.role,
    filename: options.file.name,
    media_type: options.file.type || 'application/octet-stream',
    total_size: options.file.size,
    chunk_size: RESUMABLE_UPLOAD_CHUNK_BYTES,
    checksum,
  })
  writeCheckpoint(key, created.id)
  return created
}

async function uploadMissingChunks(
  session: VideoUploadSessionResponse,
  options: ProjectAssetUploadOptions,
) {
  const missing = [...session.missing_chunks]
  let cursor = 0
  let uploadedBytes = session.received_size
  let stopped = false
  const report = () => {
    const ratio = options.file.size ? uploadedBytes / options.file.size : 0
    options.onProgress?.({
      phase: 'uploading',
      fraction: Math.min(0.95, 0.15 + ratio * 0.8),
      message: `正在上传分片 ${Math.min(session.total_chunks, session.total_chunks - missing.length + cursor)}/${session.total_chunks}`,
    })
  }
  report()

  async function worker() {
    while (!stopped) {
      ensureNotAborted(options.signal)
      const position = cursor
      cursor += 1
      const chunkIndex = missing[position]
      if (chunkIndex == null) {
        return
      }
      const start = chunkIndex * session.chunk_size
      const chunk = options.file.slice(start, Math.min(options.file.size, start + session.chunk_size))
      try {
        const checksum = await sha256Chunk(chunk)
        ensureNotAborted(options.signal)
        await contentApi.putVideoUploadChunk(session.id, chunkIndex, {
          chunk,
          checksum_sha256: checksum,
          signal: options.signal,
        })
        uploadedBytes += chunk.size
        report()
      }
      catch (error) {
        stopped = true
        throw error
      }
    }
  }

  const concurrency = Math.min(3, missing.length)
  await Promise.all(Array.from({ length: concurrency }, worker))
}

async function resumableUpload(options: ProjectAssetUploadOptions) {
  const key = checkpointKey(options)
  options.onProgress?.({ phase: 'hashing', fraction: 0, message: '正在计算完整文件 SHA-256' })
  const checksum = await sha256Blob(options.file, {
    signal: options.signal,
    onProgress: (completed, total) => options.onProgress?.({
      phase: 'hashing',
      fraction: total ? completed / total * 0.14 : 0,
      message: `正在校验文件 ${Math.round(total ? completed / total * 100 : 0)}%`,
    }),
  })
  ensureNotAborted(options.signal)
  const session = await resolveUploadSession(options, checksum, key)
  options.onSession?.(session, key)
  if (session.status === 'completed') {
    const asset = await existingAsset(session)
    if (!asset) {
      throw new Error('已完成的上传会话未找到对应工程素材')
    }
    clearUploadCheckpoint(key)
    return asset
  }
  await uploadMissingChunks(session, options)
  ensureNotAborted(options.signal)
  options.onProgress?.({ phase: 'finalizing', fraction: 0.97, message: '正在合并分片并登记素材' })
  const finalized = await contentApi.completeVideoUpload(session.id)
  clearUploadCheckpoint(key)
  options.onProgress?.({ phase: 'finalizing', fraction: 1, message: '上传完成' })
  return finalized.asset
}

async function directUpload(options: ProjectAssetUploadOptions) {
  options.onProgress?.({ phase: 'uploading', fraction: 0, message: '正在直传素材' })
  return contentApi.uploadVideoProjectAsset(options.projectId, {
    file: options.file,
    role: options.role,
    signal: options.signal,
    onProgress: (loaded, total) => options.onProgress?.({
      phase: 'uploading',
      fraction: total ? Math.min(0.99, loaded / total) : 0,
      message: `正在直传素材 ${Math.round(total ? loaded / total * 100 : 0)}%`,
    }),
  })
}

export async function uploadProjectAsset(options: ProjectAssetUploadOptions): Promise<VideoProjectAsset> {
  ensureNotAborted(options.signal)
  if (options.file.size < RESUMABLE_UPLOAD_THRESHOLD_BYTES) {
    return directUpload(options)
  }
  return resumableUpload(options)
}

export async function cancelProjectAssetUpload(uploadId: string, key?: string) {
  const response = await contentApi.cancelVideoUpload(uploadId)
  if (key) {
    clearUploadCheckpoint(key)
  }
  return response
}
