import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { extname, join, relative } from 'node:path'
import process from 'node:process'

const root = process.cwd()
const sources = [join(root, 'src/api/agent'), join(root, 'src/views/agent')]
const failures = []

function filesUnder(directory) {
  if (!existsSync(directory)) {
    return []
  }
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? filesUnder(path) : [path]
  })
}

for (const file of sources.flatMap(filesUnder).filter(path => ['.ts', '.vue'].includes(extname(path)))) {
  const source = readFileSync(file, 'utf8')
  const display = relative(root, file)
  if (source.includes('/admin-api')) {
    failures.push(`${display}: 智能体模块不得复用公司 /admin-api 链路`)
  }
  for (const match of source.matchAll(/from\s+['"]@\/api\/([^'"]+)['"]/g)) {
    if (!match[1].startsWith('agent/')) {
      failures.push(`${display}: 禁止直接依赖公司业务 API：@/api/${match[1]}`)
    }
  }
  if (/api\/agent\/meeting|views\/agent\/meeting|meeting-agent-api/.test(source)) {
    failures.push(`${display}: 个性化内容模块不得依赖会议模块`)
  }
}

if (!existsSync(join(root, 'src/api/agent/content.ts'))) {
  failures.push('缺少内容客户端 src/api/agent/content.ts')
}
else {
  const contentClient = readFileSync(join(root, 'src/api/agent/content.ts'), 'utf8')
  if (contentClient.includes('meeting-agent-api')) {
    failures.push('内容客户端跨越了会议服务边界')
  }
}

const routes = readFileSync(join(root, 'src/router/modules/remaining.ts'), 'utf8')
const contentRoutesPath = join(root, 'src/router/modules/contentRoutes.ts')
const contentRoutes = existsSync(contentRoutesPath) ? readFileSync(contentRoutesPath, 'utf8') : ''
const viteConfig = readFileSync(join(root, 'vite.config.mts'), 'utf8')
if (!contentRoutes.includes('path: \'content\'')) {
  failures.push('内容独立路由不完整')
}
if (routes.includes('path: \'meeting\'') || existsSync(join(root, 'src/views/agent/MeetingWorkbench.vue'))) {
  failures.push('内容前端不得包含会议纪要路由或页面')
}
if (!viteConfig.includes('/content/api')) {
  failures.push('内容反向代理前缀不完整')
}
if (viteConfig.includes('\'/meeting-agent-api\'')) {
  failures.push('内容前端不得包含会议服务代理')
}

if (failures.length) {
  console.error(failures.join('\n'))
  process.exitCode = 1
}
else {
  console.info('Agent boundary check passed: content-only routes, client and proxy are isolated.')
}
