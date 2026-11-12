// Login callbacks may return to an application route, never to an arbitrary URL.
export function loginRedirectPath(value: unknown) {
  if (
    typeof value !== 'string' ||
    !value.startsWith('/') ||
    value.startsWith('//') ||
    /[\\\u0000-\u0020\u007f]/u.test(value) ||
    /%2f|%5c/iu.test(value.split(/[?#]/u)[0])
  ) {
    return '/'
  }
  try {
    const target = new URL(value, 'https://local.invalid')
    if (target.origin !== 'https://local.invalid' || target.pathname === '/login') return '/'
    return `${target.pathname}${target.search}${target.hash}`
  }
  catch {
    return '/'
  }
}
