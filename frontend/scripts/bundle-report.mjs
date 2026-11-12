// JavaScript needed to open the workbench: the entry chunk plus the workbench route
// chunk and every chunk they import statically (lazy pages are not counted).
// Usage: node scripts/bundle-report.mjs dist-server
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import process from 'node:process'
import { gzipSync } from 'node:zlib'

const dist = process.argv[2] || 'dist-server'
const assets = join(dist, 'assets')
const files = readdirSync(assets).filter(name => name.endsWith('.js'))
const entry = /src="[^"]*\/assets\/([^"]+\.js)"/.exec(readFileSync(join(dist, 'index.html'), 'utf8'))?.[1]
const workbench = files.find(name => name.startsWith('ContentWorkbench-'))
const staticImport = /(?:import|from)\s*["']\.\/([^"']+\.js)["']/g

function closure(start) {
  const seen = new Set()
  const queue = [start]
  while (queue.length) {
    const name = queue.pop()
    if (!name || seen.has(name)) {
      continue
    }
    seen.add(name)
    for (const match of readFileSync(join(assets, name), 'utf8').matchAll(staticImport)) {
      queue.push(match[1])
    }
  }
  return seen
}

function size(names) {
  let raw = 0
  let gzip = 0
  for (const name of names) {
    const content = readFileSync(join(assets, name))
    raw += content.length
    gzip += gzipSync(content).length
  }
  // Rounded kilobytes; a shift keeps this integer-only.
  return { files: names.size, raw_kb: (raw + 512) >> 10, gzip_kb: (gzip + 512) >> 10 }
}

const shell = closure(entry)
const page = new Set([...shell, ...closure(workbench)])
const report = {
  entry,
  workbench,
  all_js: size(new Set(files)),
  entry_only: size(shell),
  open_workbench: size(page),
  workbench_route_extra: size(new Set([...page].filter(name => !shell.has(name)))),
}
console.info(JSON.stringify(report, null, 2))
