interface ImageCapability {
  enabled?: boolean
  parameters?: Record<string, unknown>
}

interface Option {
  label: string
  value: string
  disabled?: boolean
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function options(parameters: Record<string, unknown>, key: 'ratios' | 'sizes') {
  const raw = parameters[key]
  const values = Array.isArray(raw) ? raw : record(raw).options
  return Array.isArray(values)
    ? [...new Set(values.filter(value => typeof value === 'string' || typeof value === 'number').map(value => String(value).trim()).filter(Boolean))]
    : []
}

function custom(parameters: Record<string, unknown>, key: 'ratio' | 'size') {
  const raw = record(parameters[`${key}s`])
  return parameters[`custom_${key}`] === true || raw.allow_custom === true || raw.custom === true
}

function optionLabel(value: string, key: 'ratio' | 'size') {
  if (value === 'adaptive') {
    return '模型自动'
  }
  if (key === 'size' && /^\d+x\d+$/i.test(value)) {
    return `${value.replace(/x/i, '×')} px`
  }
  return value
}

function customValueError(parameters: Record<string, unknown>, key: 'ratio' | 'size', value: string) {
  const match = key === 'ratio' ? /^([1-9]\d{0,4}):([1-9]\d{0,4})$/.exec(value) : /^(\d{2,5})[x×](\d{2,5})$/i.exec(value)
  if (!match || Number(match[1]) <= 0 || Number(match[2]) <= 0) {
    return key === 'ratio' ? '自定义比例请填写“宽:高”，例如 9:16。' : '自定义分辨率请填写“宽x高”，例如 2048x2048。'
  }
  // eslint-disable-next-line financial/no-division -- Image aspect ratio, not a financial amount.
  const aspect = Number(match[1]) / Number(match[2])
  const range = record(parameters.aspect_ratio_range)
  if ((typeof range.min === 'number' && aspect < range.min) || (typeof range.max === 'number' && aspect > range.max)) {
    return '自定义宽高比超出当前图片模型支持范围，请重新选择。'
  }
  if (key === 'size') {
    const pixels = Number(match[1]) * Number(match[2])
    if ((typeof parameters.minimum_pixels === 'number' && pixels < parameters.minimum_pixels)
      || (typeof parameters.maximum_pixels === 'number' && pixels > parameters.maximum_pixels)) {
      return '自定义分辨率的总像素超出当前图片模型支持范围，请重新选择。'
    }
  }
  return ''
}

/** Reads the same ratios/sizes/custom_* contract as the separate image stage. */
export function imageCandidateParameters(capability: ImageCapability | undefined, current: { ratio: string, size: string }) {
  const parameters = capability?.parameters || {}
  const ratioValues = options(parameters, 'ratios')
  const sizeValues = options(parameters, 'sizes')
  const allowCustomRatio = custom(parameters, 'ratio')
  const allowCustomSize = custom(parameters, 'size')
  const declared = (key: 'ratio' | 'size') => Object.hasOwn(parameters, `${key}s`) || Object.hasOwn(parameters, `custom_${key}`)
  let error = !capability
    ? '尚未读取到图片模型参数，请重新读取后再生成。'
    : capability.enabled === false
      ? '当前图片模型未启用，请联系管理员检查服务配置。'
      : !declared('ratio') || !declared('size') ? '当前图片模型未声明完整的比例和清晰度参数，请重新读取或联系管理员。' : ''
  const visibleOptions = (key: 'ratio' | 'size', values: string[], allowCustom: boolean): Option[] => {
    const value = String(current[key] || '').trim()
    const result: Option[] = values.map(item => ({ label: optionLabel(item, key), value: item }))
    if (!value || values.includes(value)) {
      return result
    }
    const unsupported = !allowCustom || Boolean(customValueError(parameters, key, value))
    result.unshift({ value, label: `${optionLabel(value, key)}${unsupported ? '（原设置，当前模型不支持）' : ''}`, disabled: unsupported })
    return result
  }
  for (const [key, values, allowCustom] of [
    ['ratio', ratioValues, allowCustomRatio],
    ['size', sizeValues, allowCustomSize],
  ] as const) {
    const value = String(current[key] || '').trim()
    if (error || !value || values.includes(value)) {
      continue
    }
    error = allowCustom
      ? customValueError(parameters, key, value)
      : `${key === 'ratio' ? '画面比例' : '输出清晰度'}“${value}”不在当前图片模型支持范围内，请重新选择或清空使用模型默认值。`
  }
  const dimensions = /^(\d{2,5})[x×](\d{2,5})$/i.exec(String(current.size || '').trim())
  const ratio = /^([1-9]\d{0,4}):([1-9]\d{0,4})$/.exec(String(current.ratio || '').trim())
  if (!error && dimensions && ratio && Number(dimensions[1]) * Number(ratio[2]) !== Number(dimensions[2]) * Number(ratio[1])) {
    error = '自定义图片尺寸与所选画面比例不一致，请修改尺寸或清空比例后重试。'
  }
  const defaults = record(parameters.defaults)
  return {
    ratioOptions: visibleOptions('ratio', ratioValues, allowCustomRatio),
    sizeOptions: visibleOptions('size', sizeValues, allowCustomSize),
    allowCustomRatio,
    allowCustomSize,
    ratioDefaultLabel: typeof defaults.ratio === 'string' && defaults.ratio !== 'adaptive' ? `模型默认（${defaults.ratio}）` : '模型默认',
    sizeDefaultLabel: typeof defaults.size === 'string' ? `模型默认（${defaults.size}）` : '模型默认',
    error,
  }
}
