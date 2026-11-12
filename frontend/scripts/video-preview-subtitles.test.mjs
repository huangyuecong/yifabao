import assert from 'node:assert/strict'
import { timelineWebVtt } from '../src/views/agent/content/video-editor/previewSubtitles.ts'

function item(content, start = 0, duration = 1, extra = {}) {
  return { content, start_seconds: start, duration_seconds: duration, enabled: true, ...extra }
}

function timeline(items, extraTracks = []) {
  return { tracks: [{ kind: 'subtitle', muted: false, items }, ...extraTracks] }
}

const tests = []
const test = (name, callback) => tests.push({ name, callback })

test('导出实际开始/时长、中文和多行正文，返回标准 WEBVTT', () => {
  assert.equal(timelineWebVtt(timeline([item('第一行\r\n第二行', 1.25, 2.5)])), 'WEBVTT\n\n1\n00:00:01.250 --> 00:00:03.750\n第一行\n第二行\n\n')
})

test('只有已启用且未静音字幕轨内联正文进入预览，不拿标签或外部文件冒充字幕', () => {
  const value = timeline([
    item('显示字幕'),
    item('已禁用', 0, 1, { enabled: false }),
    item(null, 0, 1, { label: '外部字幕.srt', project_asset_id: 'external' }),
    item(' \r\n\t '),
  ], [
    { kind: 'subtitle', muted: true, items: [item('已静音')] },
    { kind: 'voice', muted: false, items: [item('配音文字')] },
    { kind: 'overlay', muted: false, items: [item('叠加文字')] },
  ])
  assert.equal(timelineWebVtt(value), 'WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n显示字幕\n\n')
  assert.equal(timelineWebVtt(timeline([])), 'WEBVTT\n\n')
})

test('转义标记和实体，正文换行不能注入第二个 cue 或 NOTE 区块', () => {
  const result = timelineWebVtt(timeline([
    item('<b>酒 & 茶</b> &lt;\r\n\r\n00:00:10.000 --> 00:00:12.000\n<script>不执行</script>\n\nNOTE 内容\u0000'),
  ]))
  assert.ok(result.includes('&lt;b&gt;酒 &amp; 茶&lt;/b&gt; &amp;lt;'))
  assert.ok(result.includes('\n\u200B\n00:00:10.000 --&gt; 00:00:12.000'))
  assert.ok(result.includes('&lt;script&gt;不执行&lt;/script&gt;'))
  assert.equal(result.split('\n\n').filter(Boolean).length, 2, '只能有 WEBVTT 头和原始一个 cue')
  assert.equal((result.match(/ --> /gu) || []).length, 1)
  assert.ok(!result.includes('\u0000'))
})

test('排序不改变原时间线，重叠字幕和相同开始时间保留稳定顺序', () => {
  const value = timeline([item('第三条', 3, 1), item('第一条', 1, 3), item('同起点第二条', 1, 1)])
  const snapshot = JSON.stringify(value)
  const result = timelineWebVtt(value)
  assert.ok(result.indexOf('第一条') < result.indexOf('同起点第二条'))
  assert.ok(result.indexOf('同起点第二条') < result.indexOf('第三条'))
  assert.equal(JSON.stringify(value), snapshot)
})

test('时戳跨分/时进位正确，小于毫秒的合法 cue 至少保留一毫秒', () => {
  const result = timelineWebVtt(timeline([
    item('跨分钟', 59.9996, 0.0001),
    item('跨小时', 3599.5, 1.125),
    item('长时间戳', 360000, 1),
  ]))
  assert.ok(result.includes('00:01:00.000 --> 00:01:00.001'))
  assert.ok(result.includes('00:59:59.500 --> 01:00:00.625'))
  assert.ok(result.includes('100:00:00.000 --> 100:00:01.000'))
})

test('过滤非法开始/时长与溢出时间，不能输出 NaN 或 Infinity 时戳', () => {
  const result = timelineWebVtt(timeline([
    item('负数开始', -1, 1),
    item('零时长', 0, 0),
    item('负数时长', 0, -1),
    item('非法开始', Number.NaN, 1),
    item('无限时长', 0, Number.POSITIVE_INFINITY),
    item('溢出', Number.MAX_VALUE, 1),
  ]))
  assert.equal(result, 'WEBVTT\n\n')
})

for (const { name, callback } of tests) {
  callback()
  console.info(`PASS ${name}`)
}
console.info(`Video preview subtitles: ${tests.length} tests passed.`)
