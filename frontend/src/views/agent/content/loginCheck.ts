import type { AutomationPurpose, BrowserPlatformSession } from '@/api/agent/automation'
import { ElMessageBox } from 'element-plus'
import { automationApi } from '@/api/agent/automation'
import { trialMe } from '@/api/agent/trial'

const LABELS: Record<BrowserPlatformSession['platform'], string> = { xianyu: '闲鱼', douyin: '抖音', xiaohongshu: '小红书', kuaishou: '快手' }

/**
 * Check the platforms' logins right before publishing, so a missing login is a prompt
 * instead of a failed publish record. Resolves true when publishing may go ahead.
 * On the online trial the server browser is incognito: a closed one has no login.
 */
export async function confirmLogins(purpose: AutomationPurpose, platforms: BrowserPlatformSession['platform'][], goLogin: () => void) {
  let sessions: BrowserPlatformSession[] = []
  try {
    sessions = (await automationApi.browserSessions(purpose)).items
  }
  catch {}
  const signedOut = trialMe.value.remote_browser ? ['login_required', 'not_opened'] : ['login_required']
  const missing = platforms.filter(platform => signedOut.includes(sessions.find(item => item.platform === platform)?.state ?? ''))
  const uncertain = platforms.filter(platform => !missing.includes(platform)
    && sessions.find(item => item.platform === platform)?.state !== 'logged_in')
  const names = (list: BrowserPlatformSession['platform'][]) => list.map(platform => LABELS[platform]).join('、')
  if (missing.length) {
    try {
      await ElMessageBox.confirm(`${names(missing)}还没有登录。请先在“平台账号”里${trialMe.value.remote_browser ? '扫码' : ''}登录，再提交发布。`, '请先登录', {
        type: 'warning',
        confirmButtonText: '去登录',
        cancelButtonText: '取消',
      })
      goLogin()
    }
    catch {}
    return false
  }
  if (uncertain.length) {
    try {
      await ElMessageBox.confirm(`暂时无法确认${names(uncertain)}是否已登录（发布浏览器未打开或页面还在加载）。没登录的话，发布会等你扫码 5 分钟，超时就记为失败。仍要提交吗？`, '登录状态未确认', {
        type: 'warning',
        confirmButtonText: '仍要提交',
        cancelButtonText: '先去登录',
        distinguishCancelAndClose: true,
      })
      return true
    }
    catch (action) {
      if (action === 'cancel') {
        goLogin()
      }
      return false
    }
  }
  return true
}
