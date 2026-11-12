import type { LibraryResourceKind } from '@/api/agent/content'

const fileGroups = [
  { kind: 'image', extensions: ['jpg', 'jpeg', 'png', 'webp'], maxBytes: 100 * 1024 * 1024, limit: '100 MiB' },
  { kind: 'video', extensions: ['mp4', 'mov', 'm4v', 'webm', 'mkv'], maxBytes: 4 * 1024 * 1024 * 1024, limit: '4 GiB' },
  { kind: 'audio', extensions: ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'flac'], maxBytes: 1024 * 1024 * 1024, limit: '1 GiB' },
  { kind: 'document', extensions: ['pdf', 'docx', 'txt', 'md', 'markdown'], maxBytes: 50 * 1024 * 1024, limit: '50 MiB' },
] as const

export const libraryFileAccept = fileGroups.flatMap(group => group.extensions.map(extension => `.${extension}`)).join(',')

export function isLibraryMediaKind(kind: LibraryResourceKind) {
  return ['image', 'logo', 'video', 'audio'].includes(kind)
}

export function inspectLibraryFile(file: Pick<File, 'name' | 'size'>, requestedKind: LibraryResourceKind): { kind: LibraryResourceKind, error?: string } {
  const extension = file.name.split('.').at(-1)?.toLowerCase() ?? ''
  const group = fileGroups.find(item => (item.extensions as readonly string[]).includes(extension))
  if (!group) {
    return { kind: requestedKind, error: '不支持此文件格式，请选择 PDF、DOCX、TXT、Markdown、常见图片、视频或音频文件' }
  }
  if (file.size === 0) {
    return { kind: requestedKind, error: '文件不能为空' }
  }
  if (file.size > group.maxBytes) {
    return { kind: requestedKind, error: `当前文件不能超过 ${group.limit}` }
  }
  const kind = group.kind === 'image' && ['logo', 'product'].includes(requestedKind)
    ? requestedKind
    : group.kind === 'document' && !isLibraryMediaKind(requestedKind)
      ? requestedKind
      : group.kind
  return { kind }
}
