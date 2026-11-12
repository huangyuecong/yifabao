import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = resolve(process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '../frontend'))
const require = createRequire(join(root, 'package.json'))
const timeout = setTimeout(() => {
  console.error('Frontend preflight timed out after 90 seconds. No services were started.')
  process.exit(1)
}, 90000)

try {
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))
  const missing = Object.keys({ ...manifest.dependencies, ...manifest.devDependencies })
    .filter(name => !existsSync(join(root, 'node_modules', name, 'package.json')))
  if (missing.length) {
    throw new Error(`Missing frontend packages: ${missing.join(', ')}`)
  }
  const configFile = ['vite.config.mts', 'vite.config.ts', 'vite.config.js']
    .map(name => join(root, name))
    .find(existsSync)
  if (!configFile) {
    throw new Error(`Missing Vite config under ${root}`)
  }
  process.chdir(root)
  // Importing Vite exercises Rollup's platform-specific native dependency.
  // Do not call resolveConfig here: it loads every Vite plugin (including visualizer)
  // and roughly duplicates the later `vite --mode dev` startup cost.
  const viteEntry = join(dirname(require.resolve('vite/package.json')), 'dist/node/index.js')
  console.log('Checking Vite and Rollup...')
  await import(pathToFileURL(viteEntry).href)
  const viteRequire = createRequire(require.resolve('vite'))
  console.log('Checking esbuild...')
  viteRequire('esbuild').transformSync('const ready: boolean = true', { loader: 'ts' })
  console.log('Checking Element Plus resolver...')
  await import(pathToFileURL(require.resolve('unplugin-vue-components/resolvers')).href)
  console.log(`Frontend dependency checks passed (${configFile}).`)
} catch (error) {
  console.error(error.stack || error.message)
  console.error('Restore dependencies from the project lockfile using pnpm install --frozen-lockfile.')
  process.exitCode = 1
} finally {
  clearTimeout(timeout)
}
