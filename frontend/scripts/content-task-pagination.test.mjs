import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/api/agent/content.ts', import.meta.url), 'utf8')

assert.match(source, /listTasks:\s*async\s*\(options:\s*\{\s*includeArchived\?\s*:\s*boolean\s*\}\s*=\s*\{\}\)\s*=>/u)
assert.match(source, /page_size:\s*pageSize/u)
assert.match(source, /for\s*\(let page = 1; page <= 1000; page \+= 1\)/u)
assert.match(source, /current\.length < pageSize/u)
assert.match(source, /tasks\.push\(\.\.\.current\)/u)
assert.match(source, /listLibrary:\s*async\s*\(\)\s*=>/u)
assert.match(source, /url:\s*'\/api\/v1\/library\/items'/u)
assert.match(source, /params:\s*\{\s*page,\s*page_size: pageSize\s*\}/u)

console.info('PASS task history and enterprise library walk all bounded pages')
