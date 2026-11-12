/** UI status must describe the response, not promise that an unavailable service works. */
export function serviceStatusPresentation(status?: string) {
  switch (status) {
    case 'available': return { label: '可使用', type: 'success' as const, neutral: false }
    case 'degraded': return { label: '部分服务受限', type: 'warning' as const, neutral: false }
    case 'unavailable': return { label: '暂不可用', type: 'danger' as const, neutral: false }
    case 'disabled': return { label: '未启用', type: 'info' as const, neutral: true }
    default: return { label: '状态待确认', type: 'info' as const, neutral: true }
  }
}
