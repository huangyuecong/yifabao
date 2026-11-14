import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  characterCount,
  copyProblems,
  defaultLimits,
  mergeLimits,
  previewChecks,
  previewCopy,
  textRuns,
} from '../src/views/agent/content/promotionPreview.ts'

const source = name => readFileSync(new URL(`../${name}`, import.meta.url), 'utf8')
const limits = mergeLimits(undefined)
const text = (length, char = '字') => char.repeat(length)

test('characters are counted the way the backend counts them', () => {
  assert.equal(characterCount('习水天辰'), 4)
  // Python len() counts code points; String.length would report 6 here.
  assert.equal(characterCount('a😀😀b'), 4)
  assert.equal(characterCount(''), 0)
})

test('#话题 tokens are separated from the surrounding text', () => {
  assert.deepEqual(textRuns('买 #雅酿 好酒'), [
    { text: '买 ', topic: false, over: false },
    { text: '#雅酿', topic: true, over: false },
    { text: ' 好酒', topic: false, over: false },
  ])
  assert.deepEqual(textRuns('#雅酿，好酒').map(run => [run.text, run.topic]), [['#雅酿', true], ['，好酒', false]])
  assert.deepEqual(textRuns('#a#b').map(run => [run.text, run.topic]), [['#a#b', true]])
  assert.deepEqual(textRuns('# 单独的井号').map(run => run.topic), [false])
})

test('characters past the limit are marked, counting from an offset when text is shared', () => {
  assert.deepEqual(textRuns('一二三四五', 3).map(run => [run.text, run.over]), [['一二三', false], ['四五', true]])
  assert.deepEqual(textRuns('abc', 5, 3).map(run => [run.text, run.over]), [['ab', false], ['c', true]])
  assert.deepEqual(textRuns('😀😀😀', 2).map(run => [run.text, run.over]), [['😀😀', false], ['😀', true]])
  assert.deepEqual(textRuns(''), [])
})

test('Kuaishou folds the title into the description, other platforms keep them apart', () => {
  const douyin = previewCopy('douyin', ' 标题 ', ' 正文 ', limits)
  assert.equal(douyin.merged, false)
  assert.equal(douyin.title.map(run => run.text).join(''), '标题')
  const kuaishou = previewCopy('kuaishou', text(10), text(995), limits)
  assert.equal(kuaishou.merged, true)
  // 10 title characters + newline + 995 body characters: the last 6 are past 1000.
  assert.equal(kuaishou.body.filter(run => run.over).map(run => run.text.length).join(), '6')
  assert.deepEqual(previewCopy('xiaohongshu', text(21), '正文', limits).title.map(run => run.over), [false, true])
})

test('counters follow what each publisher enforces', () => {
  const shape = checks => checks.map(check => [check.key, check.count, check.limit])
  assert.deepEqual(shape(previewChecks('douyin', '标题', '正文内容', limits)), [['title', 2, 30], ['body', 4, 1000]])
  assert.equal(previewChecks('xiaohongshu', '标题', '正文', limits)[0].limit, 20)
  // One text box on Kuaishou: title + newline + body share the 1000 character limit.
  assert.deepEqual(shape(previewChecks('kuaishou', '标题', '正文内容', limits)), [['title', 2, 30], ['caption', 7, 1000]])
})

test('copy problems match the server limits exactly at the boundaries', () => {
  const problems = (platform, title, body) => copyProblems(platform, platform, { title, body }, limits)
  assert.deepEqual(problems('xiaohongshu', text(20), text(1000)), [])
  assert.equal(problems('xiaohongshu', text(21), '正文').length, 1)
  assert.match(problems('xiaohongshu', text(21), '正文')[0], /标题 21 字.*20 字限制/)
  assert.deepEqual(problems('douyin', text(30), text(1000)), [])
  assert.equal(problems('douyin', text(31), '正文').length, 1)
  assert.equal(problems('douyin', '标题', text(1001)).length, 1)
  assert.deepEqual(problems('kuaishou', text(10), text(989)), [])
  assert.match(problems('kuaishou', text(10), text(990))[0], /标题\+正文 1001 字/)
  // Surrounding whitespace is stripped by the publisher before it counts.
  assert.deepEqual(problems('xiaohongshu', `  ${text(20)}  `, `\n${text(1000)}\n`), [])
})

test('live platform limits replace the defaults and bad values fall back', () => {
  const merged = mergeLimits([
    { id: 'xiaohongshu', title_limit: 18, description_limit: 800 },
    { id: 'douyin', title_limit: 0, description_limit: Number.NaN },
    { id: 'xianyu', title_limit: 5, description_limit: 5 },
  ])
  assert.deepEqual(merged.xiaohongshu, { title: 18, body: 800 })
  assert.deepEqual(merged.douyin, defaultLimits.douyin)
  assert.deepEqual(merged.kuaishou, defaultLimits.kuaishou)
  assert.deepEqual(mergeLimits(undefined), defaultLimits)
})

test('fallback limits still match the backend platform catalog', () => {
  const backend = source('../backend/services/content_service/src/content_service/automation_platforms.py')
  const spec = id => backend.split('PlatformSpec(').find(part => part.trimStart().startsWith(`"${id}"`))
  assert.match(backend, /title_limit: int = 30/)
  assert.match(backend, /description_limit: int = 1000/)
  assert.match(spec('xiaohongshu'), /title_limit=20/)
  // Douyin and Kuaishou rely on the 30 / 1000 defaults.
  assert.doesNotMatch(spec('douyin'), /_limit=/)
  assert.doesNotMatch(spec('kuaishou'), /_limit=/)
  assert.deepEqual(defaultLimits, {
    douyin: { title: 30, body: 1000 },
    xiaohongshu: { title: 20, body: 1000 },
    kuaishou: { title: 30, body: 1000 },
  })
})

test('preview components compile and keep their interaction guards', async () => {
  const { parse, compileTemplate } = await import('vue/compiler-sfc')
  for (const name of ['PromotionPhone', 'PromotionPreview', 'PreviewIcon']) {
    const file = `src/views/agent/content/${name}.vue`
    const { descriptor, errors } = parse(source(file))
    assert.deepEqual(errors, [], file)
    const result = compileTemplate({ source: descriptor.template.content, filename: file, id: file })
    assert.deepEqual(result.errors, [], file)
  }
  // A kept-alive page must not leave a video playing behind another page.
  assert.match(source('src/views/agent/content/PromotionPhone.vue'), /onDeactivated\(\(\) => video\.value\?\.pause\(\)\)/)

  const view = source('src/views/agent/content/CatalogPublishView.vue')
  // This page is promotion only (闲鱼上架 has its own wizard), so the preview is always there.
  assert.match(view, /<PromotionPreview\s/)
  assert.doesNotMatch(source('src/views/agent/content/CatalogListingWizard.vue'), /PromotionPreview/)
  // The preview button must neither toggle the row's selection with the mouse nor with the space bar.
  assert.match(view, /@click\.stop="preview\(content\)"\s+@keydown\.space\.stop/)
  // Only the eye button picks what is previewed: ticking rows never loads their videos.
  assert.match(view, /const previewContent = computed\(\(\) => contents\.value\.find\(item => item\.id === focusId\.value\) \?\? null\)/)
  assert.doesNotMatch(view.slice(view.indexOf('function toggle('), view.indexOf('function togglePage(')), /focusId/)
  // Copy the server would reject (422) is explained up front and blocks the submit button.
  assert.match(view, /platforms\.value\.length > 0 && !copyIssues\.value\.length/)
})
