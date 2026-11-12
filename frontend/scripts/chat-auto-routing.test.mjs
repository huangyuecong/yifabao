import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
// eslint-disable-next-line test/no-import-node-test
import test from 'node:test'
import ts from 'typescript'
import * as Vue from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const dataModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const bridge = { chat: {}, models: {}, route: { query: {} }, notices: [], timers: new Map(), nextTimer: 0 }
bridge.schedule = (callback) => {
  const id = ++bridge.nextTimer
  bridge.timers.set(id, callback)
  return id
}
globalThis.__chatAutoRoutingTest = bridge
globalThis.document = { activeElement: null }
const moduleUrls = {
  'vue': import.meta.resolve('vue'),
  'markdown-it': import.meta.resolve('markdown-it'),
  '@element-plus/icons-vue': import.meta.resolve('@element-plus/icons-vue'),
  'element-plus': dataModule('export const ElMessage = Object.fromEntries(["success", "warning", "error"].map(kind => [kind, text => globalThis.__chatAutoRoutingTest.notices.push([kind, text])])); export const ElMessageBox = { confirm: async () => true };'),
  'vue-router': dataModule('export const useRoute = () => globalThis.__chatAutoRoutingTest.route; export const useRouter = () => ({ replace: async ({ query }) => { globalThis.__chatAutoRoutingTest.route.query = query } });'),
  '@/api/agent/chat': dataModule('export const chatApi = new Proxy({}, { get: (_, method) => (...args) => globalThis.__chatAutoRoutingTest.chat[method](...args) }); export const streamReply = (...args) => (globalThis.__chatAutoRoutingTest.chat.streamReply || (() => Promise.reject(new Error("stream unavailable"))))(...args);'),
  '@/api/agent/aiModels': dataModule('export const aiModelsApi = new Proxy({}, { get: (_, method) => (...args) => globalThis.__chatAutoRoutingTest.models[method](...args) });'),
  '@/api/agent/http': dataModule('export const saveBlob = () => {};'),
  './contentWorkflow': dataModule('export const formatDate = value => String(value || ""); export const parseContentDateTime = value => (value ? new Date(value) : null);'),
  './voiceInput': dataModule(`import { ref } from "${import.meta.resolve('vue')}"; export const useVoiceInput = () => ({ state: ref('idle'), seconds: ref(0), active: ref(false), supported: ref(false), start() {}, stop() {}, cancel() {}, toggle() {} });`),
  './AgentTrace.vue': dataModule('export default { name: "AgentTrace", props: ["plan", "steps", "actions", "live"], render: () => null };'),
  './aiModelForm': dataModule(transpile(readFileSync(new URL('../src/views/agent/content/aiModelForm.ts', import.meta.url), 'utf8'))),
}
async function component(filename) {
  const source = readFileSync(new URL(`../src/views/agent/content/${filename}.vue`, import.meta.url), 'utf8')
  const compiled = compileScript(parse(source).descriptor, { id: filename, inlineTemplate: true })
  const replaced = compiled.content.replace(/from\s+(['"])([^'"]+)\1/gu, (match, quote, specifier) => {
    assert.ok(moduleUrls[specifier], `Unexpected import: ${specifier}`)
    return `from ${quote}${moduleUrls[specifier]}${quote}`
  })

  const timers = filename === 'ContentChatView'
    ? 'const setTimeout = callback => globalThis.__chatAutoRoutingTest.schedule(callback); const clearTimeout = id => globalThis.__chatAutoRoutingTest.timers.delete(id);\n'
    : ''
  // The module is compiled from the two fixed repository components above; no user input is evaluated.
  // eslint-disable-next-line no-unsanitized/method
  return (await import(dataModule(timers + transpile(replaced)))).default
}
const Chat = await component('ContentChatView')
const Models = await component('ContentChatModels')
const descendants = target => [target, ...target.children.flatMap(descendants)]
const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null, tagName: type.toUpperCase(), scrollHeight: 2400, scrollTop: 0, clientHeight: 600, addEventListener() {}, removeEventListener() {}, scrollTo(value) {
  this.scrollTop = Math.min(value.top, this.scrollHeight - this.clientHeight)
  bridge.scrolls?.push({ top: value.top, renderedMessages: descendants(this).some(item => item.props.class === 'message-content') })
}, focus() {} })
const renderer = Vue.createRenderer({
  createElement: node,
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  setText: (target, text) => {
    target.text = text
  },
  setElementText: (target, text) => {
    target.text = text
    target.children = []
  },
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  patchProp: (target, key, previous, next) => {
    target.props[key] = next
  },
  insert(target, parent, anchor = null) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, target)
  },
  remove(target) {
    if (target.parent) {
      target.parent.children.splice(target.parent.children.indexOf(target), 1)
    }
    target.parent = null
  },
})
const textOf = target => target.text + target.children.map(textOf).join('')
async function settle() {
  for (let index = 0; index < 8; index++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
function mount(Component) {
  const root = node('root')
  const app = renderer.createApp(Component)
  for (const name of ['ElButton', 'ElDrawer', 'ElDialog', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput', 'ElAlert', 'ElIcon', 'ElSkeleton', 'ElEmpty', 'ElCheckbox', 'ElSwitch']) {
    app.component(name, Vue.defineComponent({
      setup: (props, { slots, attrs }) => () => Vue.h(name, attrs, ['ElDrawer', 'ElDialog'].includes(name) && !attrs.modelValue ? [] : [...(slots.default?.() ?? []), ...(slots.footer?.() ?? [])]),
    }))
  }
  app.directive('dompurify-html', {})
  app.directive('loading', {})
  app.config.warnHandler = () => {}
  app.mount(root)
  return {
    root,
    all: type => descendants(root).filter(item => item.type === type),
    button: label => descendants(root).find(item => item.type === 'ElButton' && textOf(item).trim() === label),
    labelled: label => descendants(root).find(item => item.props['aria-label'] === label),
    close: () => app.unmount(),
  }
}
const model = (id, kind = 'chat') => ({ id, name: id, model: id, kind, provider_source: kind === 'chat' ? 'text' : kind, enabled: true, configured: true, supports_images: true, supports_video: false, supports_image_edit: kind === 'image' })
const conversation = (messages = []) => ({ id: 'conversation-1', title: '连续对话', model_id: 'old-image-model', messages, created_at: '2026-10-10T00:00:00Z', updated_at: '2026-10-10T00:00:00Z', message_count: messages.length })
const documentArtifact = { id: 'document-1', kind: 'docx', name: '新闻稿.docx', status: 'ready', text_preview: '原文内容', size: 100, mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }
const documentMessage = { id: 'assistant-1', role: 'assistant', model_id: 'text-model', content: '已生成文件', attachments: [], artifacts: [documentArtifact], status: 'completed' }
function chatApi(options = {}) {
  bridge.scrolls = []
  bridge.route = { query: options.current ? { conversation: 'conversation-1' } : {} }
  bridge.notices = []
  bridge.timers.clear()
  const calls = []
  let current = conversation(options.current ? [documentMessage] : [])
  bridge.chat = {
    models: async () => ({ models: options.models || [model('text-model'), model('old-image-model', 'image')], max_attachment_bytes: 50 * 1024 * 1024, supported_extensions: ['doc', 'docx', 'pdf', 'png'] }),
    conversations: async () => ({ items: [], total: 0 }),
    conversation: async () => structuredClone(current),
    createConversation: async (payload) => {
      calls.push(['create', payload])
      return structuredClone(current)
    },
    send: async (id, payload) => {
      calls.push(['send', id, payload])
      current = conversation([...current.messages, { id: `user-${calls.length}`, role: 'user', content: payload.content, attachments: payload.attachment_ids.map(id => ({ id })), status: 'completed' }, { ...documentMessage, id: `assistant-${calls.length}`, artifacts: [] }])
      return structuredClone(current)
    },
    retry: async (...args) => {
      calls.push(['retry', ...args])
      return structuredClone(current)
    },
    upload: async (file) => {
      calls.push(['upload', file.name])
      return { id: 'upload-1', kind: 'document', status: 'ready', name: file.name, size: file.size }
    },
    deleteAttachment: async () => {},
  }
  return calls
}
async function enter(view, text) {
  view.all('textarea')[0].props['onUpdate:modelValue'](text)
  await settle()
  assert.equal(view.button('发送').props.disabled, false)
  await view.button('发送').props.onClick()
  await settle()
}

async function poll() {
  const [id, callback] = bridge.timers.entries().next().value || []
  assert.ok(callback, 'a pending generation schedules a status poll')
  bridge.timers.delete(id)
  await callback()
  await settle()
}

test('successive requests are routed automatically without carrying an earlier image model', async () => {
  const calls = chatApi()
  const view = mount(Chat)
  await settle()
  assert.equal(view.all('ElSelect').length, 0)
  assert.equal(view.all('textarea').length, 1)
  assert.ok(view.button('添加附件'))
  assert.ok(view.button('发送'))
  await enter(view, '生成一张产品海报')
  await enter(view, '把刚才的介绍改成 PDF 给我')
  assert.deepEqual(calls.find(call => call[0] === 'create'), ['create', {}])
  for (const call of calls.filter(call => call[0] === 'send')) {
    assert.equal(call[2].output, 'auto')
    assert.equal(call[2].model_id, undefined)
    assert.equal(call[1], 'conversation-1')
  }
  await view.button('重新生成').props.onClick()
  assert.deepEqual(calls.at(-1), ['retry', 'conversation-1'])
  view.close()
})

test('continuing a Word artifact allows a PDF request and local conversion even with no configured model', async () => {
  const calls = chatApi({ current: true, models: [] })
  const view = mount(Chat)
  await settle()
  await view.button('继续修改').props.onClick()
  await enter(view, '把这个直接变成 pdf 格式给我')
  const payload = calls.find(call => call[0] === 'send')[2]
  assert.equal(payload.reference_artifact_id, 'document-1')
  assert.equal(payload.output, 'auto')
  assert.equal(payload.model_id, undefined)
  view.close()
})

test('a legacy DOC can be dropped directly and is included in the next request', async () => {
  const calls = chatApi()
  const view = mount(Chat)
  await settle()
  assert.ok(view.all('input').find(item => item.props.type === 'file').props.accept.split(',').includes('.doc'))
  view.labelled('AI 对话').props.onDrop({ preventDefault() {}, dataTransfer: { files: [new File(['test'], '资料.doc', { type: 'application/msword' })] } })
  await settle()
  assert.deepEqual(calls[0], ['upload', '资料.doc'])
  await enter(view, '根据这份文档制作新闻稿')
  assert.deepEqual(calls.find(call => call[0] === 'send')[2].attachment_ids, ['upload-1'])
  view.close()
})

for (const [status, message] of [[404, '生成文件不存在或不属于当前对话'], [410, '参考图片已丢失，请重新上传']]) {
  test(`reference failure ${status} preserves a repeated prompt instead of treating the previous reply as this submission`, async () => {
    chatApi({ current: true })
    const prompt = '把这个直接变成 pdf 格式给我'
    const previous = conversation([{ id: 'previous-user', role: 'user', content: prompt, attachments: [], status: 'completed' }, documentMessage])
    bridge.chat.conversation = async () => structuredClone(previous)
    bridge.chat.send = async () => {
      throw Object.assign(new Error(message), { status })
    }
    const view = mount(Chat)
    await settle()
    await view.button('继续修改').props.onClick()
    await enter(view, prompt)
    assert.equal(view.all('textarea')[0].value, prompt)
    assert.ok(view.labelled('移除参考产物'))
    const error = view.all('div').find(item => item.props.class === 'send-error')
    assert.ok(textOf(error).includes(message))
    assert.match(textOf(error), /检查或重新上传文件，再点击发送/u)
    assert.doesNotMatch(textOf(error), /回复旁重试/u)
    assert.equal(view.button('发送').props.disabled, false)
    await view.labelled('移除参考产物').props.onClick()
    await settle()
    assert.equal(view.labelled('移除参考产物'), undefined)
    assert.equal(view.all('textarea')[0].value, prompt)
    view.close()
  })
}

test('new user-message identity recovers a genuinely accepted response after the connection failed', async () => {
  chatApi({ current: true })
  const prompt = '把这个直接变成 pdf 格式给我'
  const previousUser = { id: 'previous-user', role: 'user', content: prompt, attachments: [], status: 'completed' }
  let current = conversation([previousUser, documentMessage])
  bridge.chat.conversation = async () => structuredClone(current)
  bridge.chat.send = async () => {
    current = conversation([...current.messages, { ...previousUser, id: 'accepted-user' }, { ...documentMessage, id: 'accepted-reply', artifacts: [] }])
    throw new Error('Network Error')
  }
  const view = mount(Chat)
  await settle()
  await view.button('继续修改').props.onClick()
  await enter(view, prompt)
  assert.equal(view.all('textarea')[0].value, '')
  assert.equal(view.labelled('移除参考产物'), undefined)
  assert.equal(view.all('div').find(item => item.props.class === 'send-error'), undefined)
  view.close()
})

test('unknown acceptance requires a successful read before another POST, while explicit rejection stays editable', async () => {
  chatApi({ current: true })
  bridge.chat.conversations = async () => ({ items: [conversation([documentMessage])], total: 1 })
  let submissions = 0
  let readsFail = false
  bridge.chat.conversation = async () => {
    if (readsFail) {
      throw new Error('对话暂时读取失败')
    }
    return conversation([documentMessage])
  }
  bridge.chat.send = async () => {
    submissions += 1
    readsFail = true
    throw new Error('Network Error')
  }
  const view = mount(Chat)
  await settle()
  await enter(view, '生成一份产品介绍')
  assert.equal(view.all('textarea')[0].value, '生成一份产品介绍')
  assert.equal(view.button('发送').props.disabled, true)
  assert.ok(view.button('刷新确认接收状态'))
  await view.button('发送').props.onClick()
  await view.button('刷新确认接收状态').props.onClick()
  await settle()
  assert.equal(submissions, 1)
  assert.equal(view.button('发送').props.disabled, true)
  readsFail = false
  const select = view.all('button').find(item => item.props.class === 'history-item')
  await select.props.onClick()
  await settle()
  assert.ok(view.button('刷新确认接收状态'), 'returning to an uncertain conversation retains its explicit recovery action')
  assert.equal(view.button('发送').props.disabled, true)
  await view.button('刷新确认接收状态').props.onClick()
  await settle()
  assert.equal(view.button('发送').props.disabled, false)
  assert.equal(view.button('刷新确认接收状态'), undefined)
  assert.equal(submissions, 1, 'refreshing never resends automatically')
  view.close()
})

test('a failed accepted message clears its submitted draft and displays the error only beside its reply', async () => {
  chatApi({ current: true })
  const prompt = '把这个直接变成 pdf 格式给我'
  const failedReply = { ...documentMessage, id: 'failed-reply', status: 'failed', error: '参考图片已丢失，请重新上传', artifacts: [] }
  bridge.chat.send = async () => conversation([
    documentMessage,
    { id: 'accepted-user', role: 'user', content: prompt, attachments: [], status: 'completed' },
    failedReply,
  ])
  const view = mount(Chat)
  await settle()
  await view.button('继续修改').props.onClick()
  await enter(view, prompt)
  assert.equal(view.all('textarea')[0].value, '')
  assert.equal(view.labelled('移除参考产物'), undefined)
  const error = view.all('div').find(item => item.props.class === 'send-error')
  assert.equal(error, undefined)
  assert.equal(view.all('div').filter(item => item.props.class === 'message-failure').length, 1)
  assert.ok(view.button('重试回复'))
  assert.equal(view.button('发送').props.disabled, true)
  view.close()
})

test('pending acceptance clears submitted attachments immediately, and later failure and retry preserve a new draft', async () => {
  const calls = chatApi()
  const prompt = '把资料做成 PDF'
  const user = { id: 'accepted-user', role: 'user', content: prompt, attachments: [{ id: 'upload-1', kind: 'document', name: '资料.doc', size: 4 }], status: 'completed' }
  const current = conversation([user, { ...documentMessage, id: 'pending-reply', status: 'pending', content: '', artifacts: [] }])
  bridge.chat.send = async () => structuredClone(current)
  bridge.chat.conversation = async () => structuredClone(current)
  let deletes = 0
  bridge.chat.deleteAttachment = async () => {
    deletes += 1
  }
  const view = mount(Chat)
  await settle()
  view.labelled('AI 对话').props.onDrop({ preventDefault() {}, dataTransfer: { files: [new File(['test'], '资料.doc')] } })
  await settle()
  await enter(view, prompt)
  assert.equal(view.all('textarea')[0].value, '')
  assert.equal(view.labelled('待发送附件'), undefined)
  assert.equal(view.all('button').filter(item => item.props.class === 'sent-attachment').length, 1)
  assert.equal(deletes, 0, 'consuming a bound attachment must not delete its server file')
  view.all('textarea')[0].props['onUpdate:modelValue']('下一条想问的问题')
  await settle()
  assert.equal(view.all('textarea')[0].props.disabled, false)
  current.messages[1] = { ...current.messages[1], status: 'failed', error: '模型暂时不可用' }
  await poll()
  assert.equal(view.all('textarea')[0].value, '下一条想问的问题')
  assert.equal(view.all('div').find(item => item.props.class === 'send-error'), undefined)
  bridge.chat.retry = async () => {
    calls.push(['retry'])
    current.messages[1] = { ...current.messages[1], id: 'retry-reply', status: 'pending', error: undefined }
    return structuredClone(current)
  }
  await view.button('重试回复').props.onClick()
  await settle()
  assert.equal(view.all('textarea')[0].value, '下一条想问的问题')
  current.messages[1] = { ...current.messages[1], status: 'failed', error: '模型仍然不可用' }
  await poll()
  assert.equal(view.all('textarea')[0].value, '下一条想问的问题')
  assert.equal(view.all('div').find(item => item.props.class === 'send-error'), undefined)
  assert.equal(calls.filter(call => call[0] === 'retry').length, 1)
  view.close()
})

test('typing while the initial submission is in flight preserves later text without changing the sent request', async () => {
  chatApi({ current: true })
  let complete
  let request
  bridge.chat.send = async (id, payload) => {
    request = payload
    return new Promise((resolve) => {
      complete = resolve
    })
  }
  const view = mount(Chat)
  await settle()
  await view.button('继续修改').props.onClick()
  view.all('textarea')[0].props['onUpdate:modelValue']('本次提交的内容')
  await settle()
  const sending = view.button('发送').props.onClick()
  await settle()
  assert.equal(view.all('textarea')[0].props.disabled, false)
  view.all('textarea')[0].props['onUpdate:modelValue']('发送过程中写好的下一条')
  await settle()
  complete(conversation([
    documentMessage,
    { id: 'new-user', role: 'user', content: request.content, attachments: [], status: 'completed' },
    { ...documentMessage, id: 'new-reply', status: 'pending', artifacts: [] },
  ]))
  await sending
  await settle()
  assert.equal(request.content, '本次提交的内容')
  assert.equal(view.all('textarea')[0].value, '发送过程中写好的下一条')
  assert.equal(view.labelled('移除参考产物'), undefined)
  view.close()
})

test('switching conversations preserves each next draft while a prior accepted generation finishes', async () => {
  chatApi({ current: true })
  const histories = {
    'conversation-1': conversation([documentMessage]),
    'conversation-2': { ...conversation(), id: 'conversation-2', title: '第二个对话' },
  }
  bridge.chat.conversations = async () => ({ items: Object.values(histories), total: 2 })
  bridge.chat.conversation = async id => structuredClone(histories[id])
  bridge.chat.send = async (id, payload) => {
    histories[id].messages.push({ id: 'new-user', role: 'user', content: payload.content, attachments: [], status: 'completed' }, { ...documentMessage, id: 'pending-reply', status: 'pending', artifacts: [] })
    return structuredClone(histories[id])
  }
  const view = mount(Chat)
  await settle()
  await enter(view, '生成一份方案')
  view.all('textarea')[0].props['onUpdate:modelValue']('第一条会话草稿')
  await settle()
  const select = title => view.all('button').find(item => item.props.class === 'history-item' && item.props.title === title).props.onClick()
  await select('第二个对话')
  await settle()
  view.all('textarea')[0].props['onUpdate:modelValue']('第二条会话草稿')
  await settle()
  histories['conversation-1'].messages.at(-1).status = 'failed'
  histories['conversation-1'].messages.at(-1).error = '生成失败'
  await select('连续对话')
  await settle()
  assert.equal(view.all('textarea')[0].value, '第一条会话草稿')
  assert.equal(view.all('div').find(item => item.props.class === 'send-error'), undefined)
  await select('第二个对话')
  await settle()
  assert.equal(view.all('textarea')[0].value, '第二条会话草稿')
  view.close()
})

test('friendly model names replace opaque service IDs in reply labels', async () => {
  const opaque = 'd9k56d4mgqo5afvbrogg0'
  chatApi({ current: true, models: [model(opaque), { ...model('text-model'), name: '雅酿创作助手' }] })
  bridge.chat.conversation = async () => conversation([
    { ...documentMessage, id: 'named', model_id: 'text-model', artifacts: [] },
    { ...documentMessage, id: 'opaque', model_id: opaque, artifacts: [] },
    { ...documentMessage, id: 'missing', model_id: 'missing', artifacts: [] },
  ])
  const view = mount(Chat)
  await settle()
  assert.deepEqual(view.all('div').filter(item => item.props.class === 'message-label').map(textOf), ['雅酿创作助手', 'AI 助手', 'AI 助手'])
  view.close()
})

test('an older PDF preview failure cannot overwrite a newly opened Word document', async () => {
  chatApi({ current: true })
  const attachment = { id: 'pdf-1', kind: 'document', name: '旧资料.pdf', mime_type: 'application/pdf', size: 100 }
  bridge.chat.conversation = async () => conversation([{ id: 'user', role: 'user', content: '', status: 'completed', attachments: [attachment] }, documentMessage])
  let rejectPreview
  bridge.chat.attachment = async () => new Promise((resolve, reject) => {
    rejectPreview = reject
  })
  const view = mount(Chat)
  await settle()
  const previous = view.all('button').find(item => item.props.class === 'sent-attachment').props.onClick()
  await settle()
  await view.button('预览').props.onClick()
  await settle()
  rejectPreview(new Error('旧 PDF 读取失败'))
  await previous
  await settle()
  assert.equal(view.all('ElDialog')[0].props.title, '新闻稿.docx')
  assert.equal(view.all('ElAlert').find(item => item.props.title === '旧 PDF 读取失败'), undefined)
  assert.ok(view.all('pre').some(item => textOf(item) === '原文内容'))
  view.close()
})

test('history opens at the last message after the loading skeleton is replaced', async () => {
  chatApi({ current: true })
  const view = mount(Chat)
  await settle()
  assert.ok(bridge.scrolls.length > 0)
  assert.ok(bridge.scrolls.every(item => item.renderedMessages && item.top === 2400))
  assert.equal(view.labelled('对话消息').scrollTop, 1800)
  view.close()
})

test('late media layout stays at the bottom but never pulls a reader down after scrolling up', async () => {
  const observers = []
  globalThis.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback
      observers.push(this)
    }

    observe() {}

    disconnect() {
      this.disconnected = true
    }
  }
  chatApi({ current: true })
  const view = mount(Chat)
  await settle()
  const list = view.labelled('对话消息')
  list.scrollHeight = 3200
  observers[0].callback()
  await settle()
  assert.equal(list.scrollTop, 2600)
  list.scrollTop = 200
  list.props.onWheelPassive({ deltaY: -200 })
  list.props.onScroll()
  const count = bridge.scrolls.length
  list.scrollHeight = 4000
  observers[0].callback()
  await settle()
  assert.equal(bridge.scrolls.length, count)
  assert.equal(list.scrollTop, 200)
  view.close()
  assert.ok(observers.every(item => item.disconnected))
  delete globalThis.ResizeObserver
})

test('video revision button sends the owned artifact reference with the next correction', async () => {
  globalThis.IntersectionObserver = class {
    observe() {}
    disconnect() {}
  }
  const calls = chatApi({ current: true })
  bridge.chat.conversation = async () => conversation([{ ...documentMessage, artifacts: [{ ...documentArtifact, id: 'video-1', kind: 'video', name: '团圆.mp4', mime_type: 'video/mp4' }] }])
  const view = mount(Chat)
  await settle()
  await view.button('调整后重做').props.onClick()
  await enter(view, '加上团圆碰杯声')
  assert.equal(calls.find(item => item[0] === 'send')[2].reference_artifact_id, 'video-1')
  view.close()
  delete globalThis.IntersectionObserver
})

test('administrators add media models alongside text models without changing the text model kind', async () => {
  let saved
  const state = { revision: 1, models: [model('text-model')], available_models: [
    { id: 'image-a', name: '生图模型 A', provider_source: 'image' },
    { id: 'image-b', name: '生图模型 B', provider_source: 'image' },
    { id: 'video-a', name: '视频模型 A', provider_source: 'video' },
  ] }
  bridge.models = { access: async () => ({ can_manage: true }) }
  bridge.chat = {
    adminModels: async () => structuredClone(state),
    saveModels: async (payload) => {
      saved = payload
      return { ...state, revision: 2, models: payload.models }
    },
  }
  const view = mount(Models)
  await settle()
  await view.button('AI 对话模型').props.onClick()
  await settle()
  assert.deepEqual(view.all('section').map(item => item.props['aria-label']), ['文本与多模态', '图片生成与修改', '视频生成'])
  assert.ok(textOf(view.labelled('视频生成')).includes('尚未添加视频模型'))
  await view.labelled('添加图片生成与修改模型').props.onClick()
  await settle()
  await view.labelled('添加图片生成与修改模型').props.onClick()
  await settle()
  await view.labelled('添加视频生成模型').props.onClick()
  await settle()
  await view.labelled('优先使用 生图模型 B').props.onClick()
  await settle()
  await view.button('保存并生效').props.onClick()
  assert.equal(saved.models.find(row => row.id === 'text-model').kind, 'chat')
  assert.deepEqual(saved.models.filter(row => row.kind === 'image').map(row => row.model), ['image-b', 'image-a'])
  assert.equal(saved.models.find(row => row.kind === 'video').model, 'video-a')
  assert.equal(view.all('ElSelect').length, 4, 'only model-ID selectors remain, types and service groups cannot overwrite one another')
  view.close()
})
