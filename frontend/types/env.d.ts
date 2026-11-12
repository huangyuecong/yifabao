/// <reference types="vite/client" />

declare module '*.vue' {
  import { DefineComponent } from '@/vue-export'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/ban-types
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ImportMetaEnv {
  readonly VITE_CONTENT_AUTH_MODE?: 'entry' | 'standalone'

  readonly VITE_APP_TITLE: string
  readonly VITE_PORT: number
  readonly VITE_OPEN: string
  readonly VITE_DEV: string
  readonly VITE_APP_CAPTCHA_ENABLE: string
  readonly VITE_APP_TENANT_ENABLE: string
  readonly VITE_APP_DEFAULT_LOGIN_TENANT: string
  readonly VITE_APP_DEFAULT_LOGIN_USERNAME: string
  readonly VITE_APP_DEFAULT_LOGIN_PASSWORD: string
  readonly VITE_APP_DOCALERT_ENABLE: string
  readonly VITE_BASE_URL: string
  readonly VITE_API_URL: string
  readonly VITE_BASE_PATH: string
  readonly VITE_DROP_DEBUGGER: string
  readonly VITE_DROP_CONSOLE: string
  readonly VITE_SOURCEMAP: string
  readonly VITE_OUT_DIR: string
  readonly VITE_AGENT_DEV_MODE: string
  readonly VITE_AGENT_DEV_USER_ID: string
  readonly VITE_AGENT_DEV_DEPARTMENT: string
  readonly VITE_AGENT_DEV_ROLES: string
  readonly VITE_CONTENT_API_URL: string
  readonly VITE_CONTENT_PROXY_TARGET: string
}

declare global {
  interface ImportMeta {
    readonly env: ImportMetaEnv
  }
}
