import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { extname, join } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'
import {
  compareContentDateTimeDescending,
  formatDate,
  parseContentDateTime,
  startOfBeijingCalendarDay,
} from '../src/views/agent/content/contentWorkflow.ts'

const legacyUtc = '2026-09-03T08:23:25.836679'
const explicitBeijing = '2026-09-03T16:23:25.836679+08:00'
const historyView = readFileSync(
  new URL('../src/views/agent/content/ContentVersionsView.vue', import.meta.url),
  'utf8',
)
const videoEditor = readFileSync(
  new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url),
  'utf8',
)
const contentHome = readFileSync(
  new URL('../src/views/agent/content/ContentHomeView.vue', import.meta.url),
  'utf8',
)
const artifactCard = readFileSync(
  new URL('../src/views/agent/content/ContentArtifactCard.vue', import.meta.url),
  'utf8',
)
const libraryCatalog = readFileSync(
  new URL('../src/views/agent/content/ContentLibraryCatalog.vue', import.meta.url),
  'utf8',
)
const servicesView = readFileSync(
  new URL('../src/views/agent/content/ContentServicesView.vue', import.meta.url),
  'utf8',
)
const contentRoot = fileURLToPath(new URL('../src/views/agent/content/', import.meta.url))
const failures = []

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolutePath = join(directory, entry.name)
    if (entry.isDirectory()) {
      return sourceFiles(absolutePath)
    }
    return ['.ts', '.vue', '.mjs', '.js'].includes(extname(entry.name)) ? [absolutePath] : []
  })
}

const contentModuleSource = sourceFiles(contentRoot)
  .map(file => readFileSync(file, 'utf8'))
  .join('\n')
const systemTimestampField
  = '(?:created_at|updated_at|completed_at|started_at|finished_at|generated_at|published_at|archived_at|failed_at|heartbeat_at|cancel_requested_at|checked_at|resolved_at|confirmed_at|crawled_at)'

function test(name, callback) {
  try {
    callback()
    console.info(`PASS ${name}`)
  }
  catch (error) {
    failures.push({ name, error })
    console.error(`FAIL ${name}`)
    console.error(error)
  }
}

test('无时区的内容服务时间按 UTC 解析并显示为北京时间', () => {
  assert.equal(parseContentDateTime(legacyUtc)?.toISOString(), '2026-09-03T08:23:25.836Z')
  assert.equal(formatDate(legacyUtc), '2026/09/03 16:23:25')
})

test('已有明确时区的数据不会被重复转换', () => {
  assert.equal(parseContentDateTime(explicitBeijing)?.toISOString(), '2026-09-03T08:23:25.836Z')
  assert.equal(formatDate(explicitBeijing), '2026/09/03 16:23:25')
})

test('历史内容日期筛选使用的解析结果与显式时区数据一致', () => {
  const legacyTime = parseContentDateTime(legacyUtc)?.getTime()
  const explicitTime = parseContentDateTime(explicitBeijing)?.getTime()
  const selectedDay = new Date(2026, 8, 3)
  const start = startOfBeijingCalendarDay(selectedDay)?.getTime()
  const endExclusive = startOfBeijingCalendarDay(selectedDay, 1)?.getTime()

  assert.equal(legacyTime, explicitTime)
  assert.equal(new Date(start).toISOString(), '2026-09-02T16:00:00.000Z')
  assert.equal(new Date(endExclusive).toISOString(), '2026-09-03T16:00:00.000Z')
  assert.ok(
    legacyTime != null
    && start != null
    && endExclusive != null
    && legacyTime >= start
    && legacyTime < endExclusive,
  )
})

test('混合时区格式按实际时刻倒序而非按字符串排序', () => {
  const earlierWithOffset = '2026-09-03T15:30:00+08:00'
  const values = [earlierWithOffset, legacyUtc]
  values.sort(compareContentDateTimeDescending)

  assert.deepEqual(values, [legacyUtc, earlierWithOffset])
})

test('历史内容筛选和排序统一复用内容时间解析器', () => {
  assert.match(historyView, /return parseContentDateTime\(task\.updated_at \|\| task\.created_at\)/u)
  assert.match(historyView, /compareContentDateTimeDescending\(/u)
  assert.match(historyView, /startOfBeijingCalendarDay\(end, 1\)/u)
  assert.doesNotMatch(historyView, /new Date\((?:left|right|task)\.updated_at/u)
})

test('视频编辑器复用内容模块的北京时间格式化', () => {
  assert.match(videoEditor, /formatDate as formatDateTime/u)
  assert.match(videoEditor, /compareContentDateTimeDescending\(left\.created_at, right\.created_at\)/u)
  assert.doesNotMatch(videoEditor, /function formatDateTime/u)
  assert.doesNotMatch(videoEditor, /String\(right\.created_at\)\.localeCompare/u)
})

test('内容模块其余系统时间展示也统一复用北京时间格式化', () => {
  assert.match(contentHome, /formatDate\(task\.updated_at \|\| task\.created_at\)/u)
  assert.match(artifactCard, /formatDate\(artifact\.created_at\)/u)
  assert.match(libraryCatalog, /formatDate\(item\.updated_at\)/u)
  assert.match(libraryCatalog, /formatDate\(scope\.row\.updated_at\)/u)
  assert.match(libraryCatalog, /formatDate\(selectedResource\.updated_at\)/u)
  assert.match(servicesView, /formatDate\(service\.checked_at\)/u)
})

test('内容模块没有绕过统一解析器直接解析或按字符串比较系统时间', () => {
  assert.doesNotMatch(
    contentModuleSource,
    new RegExp(`(?:new Date|Date\\.parse)\\s*\\([^\\n)]*\\b${systemTimestampField}\\b`, 'u'),
  )
  const rawTimestampComparisons = contentModuleSource
    .split('\n')
    .filter(line => new RegExp(`\\b${systemTimestampField}\\b`, 'u').test(line))
    .filter(line => /localeCompare|String\s*\(/u.test(line))
  assert.deepEqual(rawTimestampComparisons, [])

  const rawTimestampInterpolations = (contentModuleSource.match(/\{\{[^}]+\}\}/gu) ?? [])
    .filter(interpolation => new RegExp(`\\b${systemTimestampField}\\b`, 'u').test(interpolation))
    .filter(interpolation => !/formatDate(?:Time)?\s*\(/u.test(interpolation))
  assert.deepEqual(rawTimestampInterpolations, [])
})

if (failures.length) {
  console.error(`Content date-time contract failed: ${failures.length} check(s) failed.`)
  process.exitCode = 1
}
else {
  console.info('Content date-time contract passed: 8 checks passed.')
}
