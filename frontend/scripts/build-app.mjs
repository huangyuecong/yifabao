import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import process from 'node:process'
import { build, loadEnv } from 'vite'
import { createVitePlugins } from '../build/vite/index.ts'
import { exclude, include } from '../build/vite/optimize.ts'

const root = process.cwd()
const mode = process.argv[2] || 'dev'
const env = loadEnv(mode, root)
const pathResolve = path => resolve(root, path)

await build({
  configFile: false,
  mode,
  root,
  base: env.VITE_BASE_PATH,
  plugins: createVitePlugins(),
  css: {
    preprocessorMaxWorkers: 2,
    preprocessorOptions: {
      scss: {
        api: 'modern',
        additionalData: '@import "@/styles/variables.scss";',
        javascriptEnabled: true,
      },
    },
  },
  resolve: {
    extensions: ['.mjs', '.js', '.ts', '.jsx', '.tsx', '.json', '.scss', '.css'],
    alias: [
      { find: 'vue-i18n', replacement: 'vue-i18n/dist/vue-i18n.cjs.js' },
      { find: /^@\//, replacement: `${pathResolve('src')}/` },
      { find: 'vue-demi', replacement: 'vue-demi/lib/index.cjs' },
    ],
  },
  build: {
    minify: 'terser',
    outDir: env.VITE_OUT_DIR || 'dist',
    sourcemap: env.VITE_SOURCEMAP === 'true',
    terserOptions: {
      maxWorkers: 2,
      compress: {
        drop_debugger: env.VITE_DROP_DEBUGGER === 'true',
        drop_console: env.VITE_DROP_CONSOLE === 'true',
      },
    },
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
    target: ['chrome89', 'es2022'],
  },
  optimizeDeps: { include, exclude },
})

writeFileSync(pathResolve(`${env.VITE_OUT_DIR || 'dist'}/content-auth-mode.json`), `${JSON.stringify({
  identity_mode: env.VITE_CONTENT_AUTH_MODE === 'entry' ? 'signed' : 'development',
  standalone: env.VITE_CONTENT_AUTH_MODE === 'standalone',
})}\n`)
