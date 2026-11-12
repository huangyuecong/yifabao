import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import {
  ContentGenerationError,
  formatContentGenerationError,
  requestContentGeneration,
} from '../src/api/agent/contentGenerationErrors.ts'

function providerError(message, code, providerStatus = 400) {
  return {
    response: {
      status: 502,
      data: { detail: { message, code: 'provider_error', provider_code: code, provider_status_code: providerStatus, provider_request_id: 'req-12345', retryable: false } },
    },
  }
}

test('具体中文原因与模型错误码优先，不被外层 502 或业务状态吞掉', () => {
  const error = providerError('输入参考图片大小超过 30MB，请缩小图片后再生成（HTTP 400）', 'InvalidParameter.ImageSize')
  const message = formatContentGenerationError(error)
  assert.match(message, /超过 30MB/u)
  assert.match(message, /InvalidParameter\.ImageSize/u)
  assert.doesNotMatch(message, /未正常返回结果|服务暂时异常/u)
})

test('通用中文占位不能盖住明确的余额错误码', () => {
  const message = formatContentGenerationError(providerError('AI 视频生成失败，请稍后重试', 'InsufficientBalance', 402))
  assert.match(message, /余额或可用额度不足/u)
  assert.match(message, /InsufficientBalance/u)
})

test('未知英文不能冒充具体中文原因，保留安全错误码供排查', () => {
  const message = formatContentGenerationError(providerError('Provider rejected request due to an unknown vendor condition', 'VendorUnexpectedState', 502))
  assert.match(message, /未正常返回结果/u)
  assert.match(message, /VendorUnexpectedState/u)
  assert.doesNotMatch(message, /Provider rejected|余额|密钥/u)
})

for (const [status, expected] of [
  [400, '参数不符合'],
  [402, '支付或账户额度不足'],
  [403, '无权'],
  [404, '不存在'],
  [408, '超时'],
  [429, '过于频繁'],
  [500, '内部错误'],
  [502, '未正常返回'],
  [503, '不可用'],
  [504, '超时'],
]) {
  test(`无明确原因时 HTTP ${status} 显示可读回退`, () => {
    assert.match(formatContentGenerationError({ response: { status, data: {} } }), new RegExp(expected, 'u'))
  })
}

test('401 区分网站登录失效与模型鉴权失败', () => {
  assert.match(formatContentGenerationError({ response: { status: 401, data: {} } }), /重新登录/u)
  assert.match(formatContentGenerationError(providerError('Unauthorized', 'AuthenticationError', 401)), /密钥/u)
  assert.doesNotMatch(formatContentGenerationError(providerError('Unauthorized', 'AuthenticationError', 401)), /重新登录/u)
})

for (const location of ['result.error_detail', 'result.error_details', 'error_detail', 'error_details']) {
  test(`异步 ${location} 保留中文原因，优先旧的泛化 job.error`, () => {
    const detail = { message: '参考视频总时长超过模型允许的 15 秒，请减少参考视频', provider_code: 'InvalidParameter.Duration' }
    const job = { error: 'AI 素材生成失败', status: 'failed', result: {} }
    const [first, second] = location.split('.')
    if (second) {
      job[first][second] = detail
    }
    else {
      job[first] = detail
    }
    assert.match(formatContentGenerationError(job), /超过模型允许的 15 秒/u)
  })
}

test('旧任务只有中文 error 仍显示具体错误', () => {
  assert.equal(formatContentGenerationError({ error: '第 2 个片段对应的源视频文件不存在，请重新导入' }), '第 2 个片段对应的源视频文件不存在，请重新导入')
})

test('旧服务英文隐私错误可读且不猜参考图片序号', () => {
  assert.match(formatContentGenerationError('InputImageSensitiveContentDetected.PrivacyInformation: image content[3] may contain real person'), /第 3 张参考图/u)
  assert.match(formatContentGenerationError('InputImageSensitiveContentDetected.PrivacyInformation: may contain a real person'), /^参考图片/u)
})

test('旧英文输出审核错误不误报为输入提示词或素材被拒绝', () => {
  const message = formatContentGenerationError('OutputVideoSensitiveContentDetected: output blocked')
  assert.match(message, /生成的结果未通过内容安全检查/u)
  assert.doesNotMatch(message, /提示词或参考素材未通过/u)
})

test('错误码与追踪号拒绝各类凭据前缀，不能截短后作为编号显示', () => {
  for (const value of ['sk_SECRET', 'AKIA123456789', 'secretVALUE', 'tokenVALUE', 'keyVALUE', 'SK-SECRET']) {
    const error = providerError('参考视频不可读取，请重新导入', value)
    error.response.data.detail.provider_request_id = value
    const result = new ContentGenerationError(error)
    assert.ok(!result.message.includes(value))
    assert.equal(result.details.provider_code, undefined)
    assert.equal(result.details.provider_request_id, undefined)
  }
})

test('中文包装的旧英语响应不直接展示原文', () => {
  const message = formatContentGenerationError('AI 图片生成失败：HTTP 400: The resolution is not supported by this model')
  assert.match(message, /清晰度不符合模型要求/u)
  assert.doesNotMatch(message, /The resolution/u)
})

test('中英文凭据、签名 URL、data URI、HTML、路径与栈都不泄漏', () => {
  const value = [
    '参考素材读取失败，请更换素材。',
    'Authorization: Bearer SECRET1 api_key="SECRET 2" Cookie="session=SECRET3" Signature=SECRET4 密钥：SECRET5',
    'https://example.invalid/private.mp4?signature=SECRET6 data:image/png;base64,SECRETDATA',
    'C:\\private\\secret.mp4 /srv/private/secret.mp4',
    '<script>SECRETHTML</script><div>素材不可用</div>',
    'Traceback: SECRETSTACK',
  ].join(' ')
  const message = formatContentGenerationError(value)
  assert.doesNotMatch(message, /SECRET|https?:|data:|<script|<div|private|Traceback/iu)
  assert.match(message, /参考素材读取失败/u)
})

test('错误码与追踪号只保存安全白名单，不允许密钥混入', () => {
  const error = providerError('模型拒绝请求，请检查参数', 'SK-SECRET12345')
  error.response.data.detail.provider_request_id = 'Bearer SECRET'
  const result = new ContentGenerationError(error)
  assert.doesNotMatch(result.message, /SECRET/u)
  assert.equal(result.details.provider_code, undefined)
  assert.equal(result.details.provider_request_id, undefined)
  const safe = new ContentGenerationError(providerError('参考视频过长，请缩短到 15 秒', 'InvalidParameter'))
  assert.equal(safe.details.provider_request_id, 'req-12345')
  assert.equal(formatContentGenerationError(safe), safe.message)
})

test('生成 wrapper 沿用同一 client 与原参数，失败只抛一次，不自动弹窗或重试', async () => {
  const config = { url: '/generate', method: 'POST', data: { prompt: '原提示词' }, timeout: 1234 }
  let count = 0
  const client = { request: async (actual) => {
    count += 1
    assert.equal(actual, config)
    throw providerError('图片格式不受支持，请改用 PNG 或 JPEG', 'InvalidParameter.ImageFormat')
  } }
  await assert.rejects(requestContentGeneration(client, config), (error) => {
    assert.ok(error instanceof ContentGenerationError)
    assert.match(error.message, /PNG 或 JPEG/u)
    return true
  })
  assert.equal(count, 1)
  assert.equal(await requestContentGeneration({ request: async () => ({ data: 123 }) }, config), 123)
})

test('两个共用入口和各生成请求启用专用处理，配音/音乐/知识库维持原路径', () => {
  const api = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')
  const task = readFileSync(new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url), 'utf8')
  const editor = readFileSync(new URL('../src/views/agent/content/video-editor/VideoEditorWorkspace.vue', import.meta.url), 'utf8')
  const http = readFileSync(new URL('../src/api/agent/http.ts', import.meta.url), 'utf8')
  assert.match(api, /\['topic', 'copy', 'image', 'video_preview'\]\.includes\(stage\) \? generationRequest : request/u)
  for (const name of ['transformCopy', 'previewTransformCopy', 'generateVideoProjectImages', 'generateVideoProjectVideo', 'createVideoProjectRender', 'createVideoProjectExport']) {
    assert.match(api.slice(api.indexOf(`  ${name}:`)).split(/\n {2}\w+:/u)[0], /generationRequest</u)
  }
  for (const name of ['generateVideoProjectVoice', 'generateVideoProjectMusic', 'getLibraryItem', 'patchArtifact']) {
    const position = api.indexOf(`  ${name}:`)
    if (position >= 0) {
      assert.doesNotMatch(api.slice(position).split(/\n {2}\w+:/u)[0], /generationRequest</u)
    }
  }
  assert.match(task, /generationJobError\(activeJob\)/u)
  assert.match(editor, /formatContentGenerationError\(aiVideoJob\.value,/u)
  assert.match(editor, /ElMessage\.error\(formatContentGenerationError\(job,/u)
  assert.doesNotMatch(http, /ContentGenerationError|formatContentGenerationError/u)
})
