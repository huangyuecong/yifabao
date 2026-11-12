/* Pure helpers for the pre-publish preview of 抖音 / 小红书 / 快手.
 * No Vue or alias imports so the rules can be unit-tested with plain node. */

export type PreviewPlatform = 'douyin' | 'xiaohongshu' | 'kuaishou'

export interface PreviewLimit { title: number, body: number }
export type PreviewLimits = Record<PreviewPlatform, PreviewLimit>

export interface TextRun { text: string, topic: boolean, over: boolean }

export interface PreviewCopy {
  /** Kuaishou has no title field: the title becomes the first line of the description. */
  merged: boolean
  title: TextRun[]
  body: TextRun[]
}

export interface PreviewCheck {
  key: 'title' | 'body' | 'caption'
  label: string
  count: number
  limit: number
}

export const previewPlatforms: PreviewPlatform[] = ['douyin', 'xiaohongshu', 'kuaishou']

/** Used until the live catalog arrives; mirrors automation_platforms.py. */
export const defaultLimits: PreviewLimits = {
  douyin: { title: 30, body: 1000 },
  xiaohongshu: { title: 20, body: 1000 },
  kuaishou: { title: 30, body: 1000 },
}

const TOPIC_STOP = /[\s#@，。！？,.!?；;：:、（）()“”"'「」]/u

/** Length as the backend counts it (Python len = code points, not UTF-16 units). */
export function characterCount(text: string): number {
  return Array.from(text).length
}

export function mergeLimits(items: Array<{ id: string, title_limit?: number, description_limit?: number }> | undefined): PreviewLimits {
  const valid = (value: unknown, fallback: number) => (typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : fallback)
  const result = {} as PreviewLimits
  for (const platform of previewPlatforms) {
    const item = items?.find(value => value.id === platform)
    result[platform] = {
      title: valid(item?.title_limit, defaultLimits[platform].title),
      body: valid(item?.description_limit, defaultLimits[platform].body),
    }
  }
  return result
}

function topicMask(chars: string[]): boolean[] {
  const mask = Array.from<boolean>({ length: chars.length }).fill(false)
  for (let index = 0; index < chars.length; index++) {
    if (chars[index] !== '#') {
      continue
    }
    let end = index + 1
    while (end < chars.length && !TOPIC_STOP.test(chars[end])) {
      end++
    }
    if (end > index + 1) {
      mask.fill(true, index, end)
      index = end - 1
    }
  }
  return mask
}

/** Split text into runs that share styling: `#话题` tokens and characters beyond the limit. */
export function textRuns(text: string, limit = Number.POSITIVE_INFINITY, offset = 0): TextRun[] {
  const chars = Array.from(text)
  const topics = topicMask(chars)
  const runs: TextRun[] = []
  chars.forEach((char, index) => {
    const over = offset + index >= limit
    const last = runs[runs.length - 1]
    if (last && last.topic === topics[index] && last.over === over) {
      last.text += char
    }
    else {
      runs.push({ text: char, topic: topics[index], over })
    }
  })
  return runs
}

/** What the platform shows for this copy, with overflow marked the way the publisher would reject it. */
export function previewCopy(platform: PreviewPlatform, title: string, body: string, limits: PreviewLimits): PreviewCopy {
  const rule = limits[platform]
  const head = title.trim()
  const text = body.trim()
  if (platform === 'kuaishou') {
    return { merged: true, title: textRuns(head, rule.title), body: textRuns(text, rule.body, characterCount(head) + 1) }
  }
  return { merged: false, title: textRuns(head, rule.title), body: textRuns(text, rule.body) }
}

/** Counters the publisher enforces. Kuaishou only has one text box, so title and body share the body limit. */
export function previewChecks(platform: PreviewPlatform, title: string, body: string, limits: PreviewLimits): PreviewCheck[] {
  const rule = limits[platform]
  const head = characterCount(title.trim())
  const text = characterCount(body.trim())
  if (platform === 'kuaishou') {
    return [
      { key: 'title', label: '标题', count: head, limit: rule.title },
      { key: 'caption', label: '标题+正文', count: head + 1 + text, limit: rule.body },
    ]
  }
  return [
    { key: 'title', label: '标题', count: head, limit: rule.title },
    { key: 'body', label: '正文', count: text, limit: rule.body },
  ]
}

export const isOver = (check: PreviewCheck) => check.count > check.limit

/** The reasons the backend would refuse this copy for the platform; empty when it fits. */
export function copyProblems(platform: PreviewPlatform, platformLabel: string, copy: { title: string, body: string }, limits: PreviewLimits): string[] {
  return previewChecks(platform, copy.title, copy.body, limits)
    .filter(isOver)
    .map(check => `${check.label} ${check.count} 字，超出${platformLabel}的 ${check.limit} 字限制`)
}
