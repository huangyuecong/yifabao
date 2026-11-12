/** Accept backend paths and already-prefixed paths without duplicating the public API prefix. */
export function agentRequestPath(value: string, baseURL: string): string {
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)) {
    return value
  }
  const base = baseURL.replace(/\/+$/, '')
  if (value === base || value.startsWith(`${base}/`) || value.startsWith(`${base}?`)) {
    return value.slice(base.length) || '/'
  }
  if (base.endsWith('/api') && value.startsWith('/api/')) {
    return value.slice('/api'.length)
  }
  return value
}

/** Media elements need a browser URL; Axios joins the same path with baseURL itself. */
export function contentMediaUrl(value: unknown, baseURL: string): string {
  const url = String(value || '')
  if (!url || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(url)) {
    return url
  }
  const base = baseURL.replace(/\/+$/, '')
  if (url.startsWith('/api/') || url === base || url.startsWith(`${base}/`)) {
    return `${base}/${agentRequestPath(url, base).replace(/^\/+/, '')}`
  }
  return url
}
