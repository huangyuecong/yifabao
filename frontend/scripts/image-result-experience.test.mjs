import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const artifactCard = readFileSync(
  new URL('../src/views/agent/content/ContentArtifactCard.vue', import.meta.url),
  'utf8',
)
const taskView = readFileSync(
  new URL('../src/views/agent/content/ContentTaskView.vue', import.meta.url),
  'utf8',
)

assert.match(artifactCard, /onMounted\(requestImagePreview\)/u)
assert.match(artifactCard, /emit\('preview', props\.artifact\)/u)
assert.match(artifactCard, /图片加载中…/u)
assert.match(artifactCard, /@dblclick="openImagePreview"/u)
assert.match(artifactCard, /@keydown\.enter="openImagePreview"/u)
assert.match(artifactCard, /v-model="imagePreviewOpen"/u)
assert.match(artifactCard, /查看\/再生成/u)

assert.match(taskView, /async function generateImageRevision\(\)/u)
assert.match(taskView, /source_artifact_id: source\.id/u)
assert.match(taskView, /reference_artifact_ids: \[source\.id\]/u)
assert.match(taskView, /本次必须执行的修改要求：\$\{instruction\}/u)
assert.match(taskView, /createIdempotencyKey\(task\.value\.id, 'image', revisionPrompt\)/u)
assert.match(taskView, /knowledge_resource_ids: imageRevisionKnowledgeResourceIds\(source\)/u)
assert.match(taskView, /count: 1/u)
assert.match(taskView, /AI 生成新图片/u)
assert.match(taskView, /示例：保持产品和 Logo 不变/u)
assert.match(taskView, /已生成新版本/u)

console.info('Image result experience contract passed.')
