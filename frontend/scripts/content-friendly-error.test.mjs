import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import process from 'node:process'
import {
  friendlyContentError,
  isReferenceImagePrivacyError,
} from '../src/views/agent/content/contentWorkflow.ts'

const videoEditor = readFileSync(
  new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url),
  'utf8',
)
const servicesView = readFileSync(
  new URL('../src/views/agent/content/ContentServicesView.vue', import.meta.url),
  'utf8',
)
const taskView = readFileSync(
  new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
  'utf8',
)
const failures = []

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

test('隐私图片错误转为可操作的中文提示并保留参考图序号', () => {
  const raw = [
    '图片模型调用失败：ProviderCallError: volcengine 返回 HTTP 400',
    'InputImageSensitiveContentDetected.PrivacyInformation',
    'The image in content[3] may contain real person',
    '（请求编号：provider-request-id）',
  ].join(' / ')

  const message = friendlyContentError(raw)

  assert.equal(
    message,
    '第 3 张参考图可能包含真实人物或其他隐私信息，AI 服务已拒绝处理。请移除该图，或改用不含真人及敏感个人信息的产品图、场景图或插画后重试',
  )
  assert.equal(isReferenceImagePrivacyError(raw), true)
  assert.doesNotMatch(message, /ProviderCallError|InputImageSensitive|may contain real person|request-id/iu)
})

test('无可靠序号时不猜测第几张参考图', () => {
  const message = friendlyContentError(
    'InputImageSensitiveContentDetected.PrivacyInformation: may contain a real person',
  )

  assert.match(message, /^参考图片可能包含/u)
  assert.doesNotMatch(message, /第 \d+ 张/u)
})

test('content[0] 是文本而不是参考图，不显示错误序号', () => {
  const message = friendlyContentError(
    'InputImageSensitiveContentDetected.PrivacyInformation at content[0]',
  )

  assert.match(message, /^参考图片可能包含/u)
  assert.doesNotMatch(message, /第 0 张/u)
})

test('其他提供商技术错误不暴露内部名称、HTTP 和请求编号', () => {
  const fallback = 'AI 视频生成失败，请稍后重试'
  const message = friendlyContentError(
    'volcengine-ark-ai-video 返回 HTTP 502：ProviderCallError / upstream rejected request（请求编号：abc-123）',
    fallback,
  )

  assert.equal(message, '内容服务暂时异常，请稍后重试')
  assert.doesNotMatch(message, /volcengine|ProviderCallError|HTTP|abc-123/iu)
})

test('媒体任务错误不暴露 FFmpeg、服务器路径或内部任务编号', () => {
  const fallback = '精确预览生成失败，请检查时间线中的素材是否仍可用后重新生成'
  const message = friendlyContentError(
    'FFmpeg 渲染失败：C:\\service\\jobs\\a0b1c2d3-e4f5-4678-9abc-0123456789ab\\output.mp4',
    fallback,
  )

  assert.equal(message, fallback)
  assert.doesNotMatch(message, /FFmpeg|C:\\|a0b1c2d3-e4f5/iu)
})

test('未识别的中文技术错误不暴露内部模型和业务状态', () => {
  const fallback = 'AI 视频生成失败，请稍后重试'
  const message = friendlyContentError(
    'wan2.1-i2v-turbo 模型调用失败（业务状态 123）',
    fallback,
  )

  assert.equal(message, fallback)
  assert.doesNotMatch(message, /wan2\.1-i2v-turbo|业务状态 123/iu)
})

test('可读中文业务错误仅移除请求编号而不丢失原因', () => {
  assert.equal(
    friendlyContentError('当前文案已更新，请刷新后重试（请求编号：abc-123）'),
    '当前文案已更新，请刷新后重试',
  )
})

test('后端中文隐私提示保留具体素材名称并禁止盲目重试', () => {
  const message
    = 'AI 视频生成未通过隐私安全检查：第 3 张参考图片“已确认图片 v1”可能包含真实人物。请移除或更换该素材后重新生成。'

  assert.equal(isReferenceImagePrivacyError(message), true)
  assert.equal(friendlyContentError(message), message)
  assert.match(friendlyContentError(message), /已确认图片 v1/u)
})

test('任务页、视频编辑器和服务状态页统一使用安全错误文案', () => {
  assert.match(videoEditor, /ElMessage\.error\(formatContentGenerationError\(job,/u)
  assert.match(videoEditor, /: friendlyContentError\(error, '请求失败，请稍后重试'\)/u)
  assert.match(videoEditor, /formatContentGenerationError\(aiVideoJob\.value,/u)
  assert.match(videoEditor, /:description="aiVideoJobErrorMessage"/u)
  assert.match(videoEditor, /&& !aiVideoJobNeedsReferenceChange/u)
  assert.doesNotMatch(videoEditor, /referencePrivacyServiceWarnings|retryableServiceWarnings/u)
  assert.doesNotMatch(videoEditor, /:title="exactPreviewJob\.error"/u)
  assert.doesNotMatch(videoEditor, />\{\{ exportJob\.error \}\}<\/p>/u)
  assert.match(videoEditor, /精确预览生成失败，请检查时间线中的素材是否仍可用后重新生成/u)
  assert.match(videoEditor, /视频导出失败，请检查主画面、音频和字幕素材后重新导出/u)
  assert.doesNotMatch(servicesView, /调用异常时页面会提示|之后可再次尝试|serviceMessage/u)
  assert.match(servicesView, /:description="loadError"/u)
  assert.doesNotMatch(servicesView, /service\.message/u)
  assert.match(taskView, /&& !isReferenceImagePrivacyError\(activeJob\.error\)/u)
})

if (failures.length) {
  console.error(`Content friendly-error contract failed: ${failures.length} check(s) failed.`)
  process.exitCode = 1
}
else {
  console.info('Content friendly-error contract passed: 9 checks passed.')
}
