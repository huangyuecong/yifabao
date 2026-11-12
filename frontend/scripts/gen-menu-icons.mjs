import { readFileSync, writeFileSync } from 'node:fs'
// 从本地 @iconify/json 全量集里，只抽取 used-icons.json 列出的图标，
// 生成一个小集合供宿主启动时离线注册（不打包整套、不依赖网络）。
// 用法：node scripts/gen-menu-icons.mjs（改完 used-icons.json 后重跑）。
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { getIcons } from '@iconify/utils'

const require = createRequire(import.meta.url)
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const usedIcons = JSON.parse(
  readFileSync(resolve(root, 'src/icons/used-icons.json'), 'utf-8'),
)

// 按前缀分组：{ tabler: ['settings', ...], 'ant-design': [...] }
const byPrefix = {}
for (const full of usedIcons) {
  const [prefix, name] = full.split(':')
  if (!prefix || !name) {
    console.warn(`跳过非法图标名：${full}（应为 prefix:name 格式）`)
    continue
  }
  ;(byPrefix[prefix] ??= []).push(name)
}

const collections = []
for (const [prefix, names] of Object.entries(byPrefix)) {
  const fullSet = require(`@iconify/json/json/${prefix}.json`)
  const subset = getIcons(fullSet, names)
  if (!subset) {
    console.warn(`集合 ${prefix} 未匹配到任何图标`)
    continue
  }
  const missing = names.filter(n => !subset.icons[n] && !subset.aliases?.[n])
  if (missing.length) {
    console.warn(`集合 ${prefix} 中未找到：${missing.join(', ')}`)
  }
  collections.push(subset)
}

const out = resolve(root, 'src/icons/menu-icons.gen.json')
writeFileSync(out, `${JSON.stringify(collections)}\n`)
console.log(
  `已生成 ${out}，共 ${collections.length} 套、`
  + `${collections.reduce((n, c) => n + Object.keys(c.icons).length, 0)} 个图标`,
)
