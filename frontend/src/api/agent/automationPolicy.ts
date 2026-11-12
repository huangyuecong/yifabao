import type { AutomationConfig, AutomationExecutionMode, AutomationPlatform, AutomationPlatformDefinition, AutomationPurpose, AutomationStatus } from './automation'

const platformPurposes: Record<AutomationPlatform, AutomationPurpose> = {
  xianyu: 'listing',
  douyin: 'promotion',
  xiaohongshu: 'promotion',
  kuaishou: 'promotion',
}

export function automationPurpose(config: AutomationConfig): AutomationPurpose | null {
  const purposes = config.platforms.map(platform => platformPurposes[platform])
  const inferred = purposes[0]
  if (!inferred || purposes.some(purpose => purpose !== inferred) || (config.purpose && config.purpose !== inferred)) {
    return null
  }
  if ((config.operation || 'publish') !== 'publish' && (config.platforms.length !== 1 || config.platforms[0] !== 'xianyu')) {
    return null
  }
  return inferred
}

export function executionMode(config: Pick<AutomationConfig, 'execution_mode'>): AutomationExecutionMode {
  return config.execution_mode || 'browser'
}

export function purposeExecutionMode(_purpose: AutomationPurpose): AutomationExecutionMode {
  return 'browser'
}

export function workflowForPurpose(config: AutomationConfig, purpose: AutomationPurpose): AutomationConfig {
  const mode = purposeExecutionMode(purpose)
  const changed = executionMode(config) !== mode
  const platforms = config.platforms.filter(platform => platformPurposes[platform] === purpose)
  return {
    ...config,
    purpose,
    execution_mode: mode,
    device_id: mode === 'android' && !changed ? config.device_id || null : null,
    accounts: changed
      ? {}
      : Object.fromEntries(
          Object.entries(config.accounts || {}).filter(([platform]) => platforms.includes(platform as AutomationPlatform)),
        ),
    platforms,
    operation: purpose === 'promotion' ? 'publish' : config.operation || 'publish',
  }
}

export function compatiblePlatforms(catalog: AutomationPlatformDefinition[], purpose: AutomationPurpose, mode: AutomationExecutionMode, operation = 'publish') {
  return catalog.filter(item => item.purpose === purpose && item.execution_modes.includes(mode) && item.operations.includes(operation as 'publish' | 'relist' | 'delist'))
}

export function changeExecutionMode(config: AutomationConfig, mode: AutomationExecutionMode, catalog: AutomationPlatformDefinition[]): AutomationConfig {
  const purpose = config.purpose || automationPurpose(config)
  const compatible = purpose && purpose !== 'creation' ? compatiblePlatforms(catalog, purpose, mode, config.operation).map(item => item.id) : []
  return {
    ...config,
    execution_mode: mode,
    platforms: config.platforms.filter(platform => compatible.includes(platform)),
    accounts: {},
    device_id: null,
    auto_publish: (config.operation || 'publish') !== 'publish' || config.auto_publish,
  }
}

export function platformLimits(platforms: AutomationPlatform[], catalog: AutomationPlatformDefinition[]) {
  const selected = catalog.filter(item => platforms.includes(item.id))
  const titleLimits = selected.map(item => item.title_limit).filter(limit => limit > 0)
  const descriptionLimits = selected.map(item => item.description_limit).filter(limit => limit > 0)
  return {
    title: titleLimits.length ? Math.min(...titleLimits) : 30,
    description: descriptionLimits.length ? Math.min(...descriptionLimits) : 1000,
  }
}

export function publishBlockReason(config: AutomationConfig, status: AutomationStatus | undefined, catalog: AutomationPlatformDefinition[]): string {
  const purpose = automationPurpose(config)
  if (!purpose) {
    return '请选择同一业务下的发布平台'
  }
  const mode = executionMode(config)
  if (mode !== purposeExecutionMode(purpose)) {
    return '此任务使用旧执行方式，请打开工作流重新生成草稿'
  }
  const allowed = compatiblePlatforms(catalog, purpose, mode, config.operation).map(item => item.id)
  if (config.platforms.some(platform => !allowed.includes(platform))) {
    return '所选平台暂不支持此执行方式或操作'
  }
  if (!status) {
    return '请先刷新执行环境状态'
  }
  if (mode === 'android') {
    const android = status.android
    if (!android?.ready) {
      return android?.missing.join('；') || '安卓 App 发布适配器待接入，当前可保存规则和生成草稿'
    }
    const device = android.devices.find(item => item.device_id === config.device_id)
    if (!device?.configured || !device.connected) {
      return '请选择已配置并连接的安卓设备'
    }
    if (config.platforms.some(platform => !device.platforms.includes(platform))) {
      return '所选设备尚未配置全部目标 App'
    }
    if (device.busy) {
      return '安卓屏幕正在使用中'
    }
    if ((config.operation || 'publish') !== 'publish') {
      return '闲鱼 App 重新上架和下架尚待账号登录后适配'
    }
    for (const platform of config.platforms) {
      const app = device.apps?.[platform]
      if (app?.state !== 'logged_in' || !app.account_id) {
        return '请先在所选设备打开 App 登录并检测账号'
      }
      if (config.accounts?.[platform] && config.accounts[platform] !== app.account_id) {
        return '发布规则中的账号与所选设备绑定账号不一致'
      }
    }
    return '可从草稿进入 App 核对发布；无人值守发布待实机验证'
  }
  const browser = status.browser || status
  for (const platform of config.platforms) {
    const platformStatus = browser.platforms?.[platform]
    const operation = config.operation || 'publish'
    const connection = platformStatus?.operations?.[operation] || (operation === 'publish' ? platformStatus : undefined)
    if (!connection?.configured) {
      return connection?.missing.join('；') || browser.missing?.join('；') || '浏览器发布连接尚未配置'
    }
    const account = config.accounts?.[platform]
    if (account && !connection.accounts.some(item => item.account_id === account && item.configured !== false)) {
      return '所选平台账号尚未配置'
    }
  }
  return ''
}
