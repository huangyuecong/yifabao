import type { ConfigEnv, UserConfig } from 'vite'
import { resolve } from 'node:path'
import dayjs from 'dayjs'
import { visualizer } from 'rollup-plugin-visualizer'
import { loadEnv } from 'vite'
import { rewriteContentApiPath } from './build/contentProxy'
import { createVitePlugins } from './build/vite'
import { exclude, include } from './build/vite/optimize'
// 当前执行node命令时文件夹的地址(工作目录)
// eslint-disable-next-line node/prefer-global/process
const root = process.cwd()

// 路径查找
function pathResolve(dir: string) {
  return resolve(root, '.', dir)
}

const lastBuildTime = dayjs().format('YYYY-MM-DD_HH-mm-ss')

// https://vitejs.dev/config/
export default ({ command, mode }: ConfigEnv): UserConfig => {
  let env = {} as any
  const isBuild = command === 'build'
  if (!isBuild) {
    // eslint-disable-next-line node/prefer-global/process
    env = loadEnv(mode, root)
  }
  else {
    env = loadEnv(mode, root)
  }
  return {
    base: env.VITE_BASE_PATH,
    root,
    // 服务端渲染
    server: {
      port: env.VITE_PORT, // 端口号
      host: env.VITE_CONTENT_AUTH_MODE === 'standalone' ? '127.0.0.1' : '0.0.0.0',
      open: env.VITE_OPEN === 'true',
      // 与服务器网关一致：/content/api/v1/... 转发到后端 /api/v1/...
      proxy: {
        '^/content/api(?:/|$)': {
          target: env.VITE_CONTENT_PROXY_TARGET || 'http://127.0.0.1:8002',
          changeOrigin: true,
          rewrite: rewriteContentApiPath,
        },
      },
      headers: {
        'content-security-policy': 'worker-src http: blob:',
      },
    },
    // 项目使用的vite插件。 单独提取到build/vite/plugin中管理,
    plugins: [
      ...createVitePlugins(),
      visualizer(),
    ],
    css: {
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
        {
          find: 'vue-i18n',
          replacement: 'vue-i18n/dist/vue-i18n.cjs.js',
        },
        {
          find: /@\//,
          replacement: `${pathResolve('src')}/`,
        },
        {
          find: 'vue-demi',
          replacement: 'vue-demi/lib/index.cjs',
        },
      ],
    },
    build: {
      minify: 'terser',
      outDir: env.VITE_OUT_DIR || 'dist',
      // sourcemap: env.VITE_SOURCEMAP === 'true' ? 'inline' : false,
      sourcemap: env.VITE_SOURCEMAP === 'true',
      // brotliSize: false,
      terserOptions: {
        compress: {
          drop_debugger: env.VITE_DROP_DEBUGGER === 'true',
          drop_console: env.VITE_DROP_CONSOLE === 'true',
        },
      },
      rollupOptions: {
        output: {
          entryFileNames: `assets/${lastBuildTime}/[name].js`,
          chunkFileNames: `assets/${lastBuildTime}/[name].js`,
          assetFileNames: `assets/${lastBuildTime}/[name].[ext]`,
        },
      },
      target: ['chrome89', 'es2022'],
    },
    optimizeDeps: { include, exclude },
  }
}
