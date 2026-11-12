import type { AxiosInstance, AxiosRequestConfig } from 'axios'

type Fields = Record<string, unknown>

function fields(value: unknown): Fields {
  return value && typeof value === 'object' ? value as Fields : {}
}

function text(value: unknown) {
  return typeof value === 'string' ? value.trim() : ''
}

function statusCode(value: unknown) {
  const numeric = Number(value)
  return Number.isInteger(numeric) && numeric >= 400 && numeric <= 599 ? numeric : undefined
}

function publicCode(value: unknown) {
  const code = text(value)
  return /^[a-z][\w.-]{2,95}$/iu.test(code) && !/^(?:sk[-_]|AKIA|secret|token|key|bearer|eyJ)/iu.test(code) ? code : ''
}

function sanitizedMessage(value: string) {
  return value
    .split(/traceback|\bstack(?:trace)?\s*[:\n]|\b(?:select|insert|update|delete)\s+(?:from|into|set|\*)/iu)[0]
    .replace(/(?:\b(?:authorization|api[ _-]?key|access[ _-]?token|token|secret|password|cookie|signature)|(?:访问)?密钥|口令|密码)["']?\s*[:=：]\s*(?:"[^"]*"|'[^']*'|(?:bearer\s+)?[^\s,;，；）)]+)/giu, '[凭据已隐藏]')
    .replace(/\bBearer\s+[^\s,;，；"'）)]+/giu, '[凭据已隐藏]')
    .replace(/\bsk-[\w-]{8,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+/giu, '[凭据已隐藏]')
    .replace(/data:[^\s<>"'）)，；]+/giu, '[内容已隐藏]')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>|<style\b[^>]*>[\s\S]*?<\/style\s*>/giu, '')
    .replace(/<[^>]*>/gu, '')
    .replace(/https?:\/\/[^\s<>"'）)，；]+/giu, '[地址已隐藏]')
    .replace(/[a-z]:[\\/][^\s<>"'）)，；]+|\/(?:tmp|var|home|app|srv)\/[^\s<>"'）)，；]+/giu, '[路径已隐藏]')
    .replace(/[（(]\s*(?:请求编号|request[ _-]?id)[^()（）]*[）)]/giu, '')
    .replace(/[\r\n\t]+/gu, ' ')
    .trim()
    .slice(0, 700)
}

function knownReason(value: string, providerFailure: boolean) {
  const lower = value.toLowerCase()
  if (/inputimagesensitivecontentdetected.*privacyinformation|may contain (?:a )?real person|reference_image_real_person/u.test(lower)) {
    const index = lower.match(/content\s*\[\s*([1-9]\d*)\s*\]/u)?.[1]
    return `${index ? `第 ${index} 张参考图` : '参考图片'}可能包含真实人物或其他隐私信息，AI 服务已拒绝处理。请移除或更换该图后重新生成`
  }
  if (/insufficient[_. -]?(?:balance|credit|quota|fund)|quota[_. -]?exceeded|arrear|overdue|payment[_. -]?required|billing|accountbalance/u.test(lower)) {
    return 'AI 服务账户余额或可用额度不足，请联系管理员检查充值和额度后再生成'
  }
  if (/invalid[_. -]?(?:api[_. -]?)?key|authentication[_. -]?(?:failed|error)|invalid[_. -]?token/u.test(lower)) {
    return 'AI 服务访问密钥无效或已过期，请联系管理员检查密钥配置'
  }
  if (/modelnotopen|model[_. -]?(?:not[_. -]?)?(?:activated|enabled)|accessdenied|permission[_. -]?denied/u.test(lower)) {
    return '当前账户没有所选模型或服务的调用权限，请联系管理员检查开通状态和授权'
  }
  if (/sensitivecontent|content[_. -]?(?:safety|policy|filter)|moderation|safety[_. -]?(?:check|violation)/u.test(lower)) {
    if (/output|generated/u.test(lower)) {
      return 'AI 生成的结果未通过内容安全检查，本次结果无法返回。请调整生成要求后重试'
    }
    return '提示词或参考素材未通过 AI 服务的内容安全检查，请调整相关内容后重新生成'
  }
  if (/(?:resolution|image[_. -]?size|aspect[_. -]?ratio).*(?:unsupported|not supported|invalid)|(?:unsupported|invalid).*(?:resolution|image[_. -]?size|aspect[_. -]?ratio)/u.test(lower)) {
    return '所选图片尺寸、比例或清晰度不符合模型要求，请选择当前模型支持的参数'
  }
  if (/timeout|timed out|etimedout|econnaborted/u.test(lower)) {
    return '生成服务响应超时，请稍后重试；已提交的任务可先查看生成状态'
  }
  if (/failed to fetch|networkerror|network error|econnrefused|connection refused/u.test(lower)) {
    return '无法连接生成服务，请检查网络和服务连接后重试'
  }
  if (/返回了空错误|返回了无法解析的错误/u.test(value)) {
    return 'AI 服务未正常返回结果，请稍后重试；如持续失败请调整参考图片或提示词后再试'
  }
  if (/rate[_. -]?limit|too many requests|throttl/u.test(lower)) {
    return 'AI 服务请求过于频繁或已达到并发上限，请稍后再生成'
  }
  if (/unauthorized/u.test(lower)) {
    return providerFailure
      ? 'AI 服务鉴权失败，请联系管理员检查访问密钥'
      : '当前登录状态无效或已过期，请重新登录后再试'
  }
  return ''
}

/** Used only by content generation, not the shared meeting/library HTTP client. */
export function formatContentGenerationError(error: unknown, fallback = '生成失败，服务未返回具体原因，请稍后重试') {
  if (error instanceof ContentGenerationError) {
    return error.message
  }
  const input = fields(error)
  const response = fields(input.response)
  const payload = fields(response.data ?? error)
  const result = fields(payload.result)
  const detail = fields(payload.error_detail ?? payload.error_details ?? result.error_detail ?? result.error_details ?? payload.detail)
  const raw = text(detail.message) || text(payload.error) || text(payload.detail)
    || text(payload.message) || text(input.message) || text(error)
  const code = publicCode(detail.provider_code) || publicCode(detail.code)
  const providerStatus = statusCode(detail.provider_status_code)
  const httpStatus = providerStatus ?? statusCode(response.status) ?? statusCode(payload.status)
    ?? statusCode(raw.match(/\b(?:HTTP(?:\/\d(?:\.\d)?)?\s*|status(?: code)?(?:[ \t]*[:=][ \t]*|[ \t]+))([45]\d{2})\b/iu)?.[1])
  const providerFailure = Boolean(providerStatus || detail.provider_code || detail.provider_request_id || detail.category)
  const safe = sanitizedMessage(raw)
  const withoutCode = code ? safe.replaceAll(code, '') : safe
  const meaningfulChinese = withoutCode.replace(/AI|HTTP\s*\d+|图片|视频|标题|文案|模型|服务|生成|调用|请求|返回|失败|异常|错误|代码|状态/giu, '')
  const englishSentence = /\b[a-z]+(?:\s+[a-z]+){2,}\b/iu.test(withoutCode)
  const generic = /^(?:AI\s*)?(?:图片|视频|标题|文案|内容|生成)?(?:服务)?(?:生成|调用)?(?:失败|暂时异常|暂时无法使用|未能创建生成任务)[，：:；\s]*(?:请(?:稍后(?:再次)?重试|重新提交或更换参考素材))?[。！]?$/u.test(safe)
  // A specific backend Chinese explanation wins over an outer HTTP 502.
  let message = /[\u3400-\u9FFF]{2}/u.test(meaningfulChinese) && !englishSentence && !generic
    ? safe
    : knownReason(`${code} ${raw}`, providerFailure)
  if (!message) {
    const defaults: Record<number, string> = {
      400: '生成参数不符合要求，请检查提示词、参考素材和所选参数',
      401: providerFailure ? 'AI 服务鉴权失败，请联系管理员检查访问密钥' : '当前登录状态无效或已过期，请重新登录后再试',
      402: 'AI 服务要求支付或账户额度不足，请联系管理员检查余额与计费状态',
      403: providerFailure ? 'AI 服务拒绝访问，请联系管理员检查模型权限和开通状态' : '当前账户无权执行此操作，请联系管理员检查权限',
      404: '请求的模型、参考素材或任务不存在，请检查所选内容和模型配置',
      408: '生成请求超时，请稍后重试',
      409: '内容版本已变化，请刷新后重新生成',
      422: '生成参数不符合要求，请检查提示词、参考素材和所选参数',
      429: 'AI 服务请求过于频繁或已达到并发上限，请稍后再生成',
      500: '生成服务发生内部错误，请稍后重试；如持续失败请联系管理员查看服务日志',
      502: '生成服务未正常返回结果，请稍后重试；如持续失败请联系管理员检查上游服务',
      503: '生成服务当前不可用，请稍后重试或联系管理员检查服务状态',
      504: '生成服务响应超时，请稍后重试；已提交的任务可先查看生成状态',
    }
    message = httpStatus ? defaults[httpStatus] || fallback : fallback
  }
  return code && !message.includes(code) ? `${message}（错误代码：${code}）` : message
}

export class ContentGenerationError extends Error {
  readonly details: Fields
  readonly outcomeUncertain: boolean

  constructor(error: unknown, fallback?: string) {
    super(formatContentGenerationError(error, fallback))
    this.name = 'ContentGenerationError'
    const response = fields(fields(error).response)
    const payload = fields(response.data ?? error)
    const result = fields(payload.result)
    const detail = fields(payload.error_detail ?? payload.error_details ?? result.error_detail ?? result.error_details ?? payload.detail)
    const status = statusCode(response.status)
    this.outcomeUncertain = !status || ([408, 500, 502, 504].includes(status)
      && !detail.provider_code && !detail.provider_status_code && !detail.category)
    const requestId = text(detail.provider_request_id)
    this.details = {
      code: publicCode(detail.code) || undefined,
      provider_code: publicCode(detail.provider_code) || undefined,
      provider_status_code: statusCode(detail.provider_status_code),
      provider_request_id: /^[a-z\d][\w-]{2,127}$/iu.test(requestId) && !/^(?:sk[-_]|AKIA|secret|token|key|bearer|eyJ)/iu.test(requestId) ? requestId : undefined,
      retryable: typeof detail.retryable === 'boolean' ? detail.retryable : undefined,
    }
  }
}

export async function requestContentGeneration<T>(client: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    return (await client.request<T>(config)).data
  }
  catch (error) {
    // Keep the provider detail before the shared client reduces it to Error.message.
    throw new ContentGenerationError(error)
  }
}
