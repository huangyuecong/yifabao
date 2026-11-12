import type { Plugin, ResolvedConfig } from 'vite'
import { cpSync, existsSync } from 'node:fs'
import { basename, resolve } from 'node:path'

export function productionPublicAssets(): Plugin {
  let config: ResolvedConfig
  return {
    name: 'production-public-assets',
    apply: 'build',
    config: () => ({ build: { copyPublicDir: false } }),
    configResolved(value) {
      config = value
    },
    writeBundle() {
      if (config.publicDir && existsSync(config.publicDir)) {
        cpSync(config.publicDir, resolve(config.root, config.build.outDir), {
          recursive: true,
          filter: source => !/^(?:_test_|TEST_WEB_)/i.test(basename(source)),
        })
      }
    },
  }
}
