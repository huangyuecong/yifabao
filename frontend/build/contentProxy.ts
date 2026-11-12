export function rewriteContentApiPath(path: string): string {
  const suffix = path.replace(/^\/content\/api(?=\/|\?|$)/, '') || '/'
  return /^\/health\/?(?:\?|$)/.test(suffix) ? suffix : `/api${suffix}`
}
