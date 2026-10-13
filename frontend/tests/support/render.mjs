/**
 * 无 DOM 的组件挂载与交互工具。
 *
 * 项目一直刻意不引 jsdom / vitest（`npm test` 只跑 `node --test`），但组件测试要真的「点一下按钮、
 * 看状态有没有跟着变」。这里用 Vue 自带的 `createRenderer`（runtime-core 的自定义渲染器）把组件渲染成
 * 一棵普通对象树：没有 DOM，但 `patchProp` 记录的 `onClick` / `onChange` 就是真的事件处理器，
 * 调它等于点它，视图重渲染也在同一棵树上看得到。
 *
 * 浏览器 API 只补组件与 API 层真正用到的那几个（localStorage / fetch / window.location /
 * document.createElement + URL.createObjectURL），补多了就成了「自己写一个浏览器」。
 */
import { createRenderer, defineComponent, h, nextTick } from 'vue'

// ---------------------------------------------------------------- 对象树渲染器

// Node 里没有 Document / ShadowRoot 这两个全局构造函数，而 runtime-dom 的 vModelText 指令
// （`v-model` 挂在原生 input 上时用它）会做 `rootNode instanceof Document` 判断当前焦点，
// 不补就抛 `ReferenceError: Document is not defined`，v-model 的每次更新都会挂掉。
if (typeof globalThis.Document !== 'function') globalThis.Document = class Document {}
if (typeof globalThis.ShadowRoot !== 'function') globalThis.ShadowRoot = class ShadowRoot {}
// 所有测试节点共用一个「假 document」：activeElement 一直是 null，于是 vModelText 走不到
// 「正在输入就不回写」那条分支，绑定的值能正常同步。
const fakeDocument = new globalThis.Document()
fakeDocument.activeElement = null

function makeNode(type, text = '') {
  // 节点要装成「像 DOM 一点点」：`v-model` 在原生 input 上会挂 runtime-dom 的 vModelText 指令，
  // 它 created 钩子里直接调 `el.addEventListener` —— 没有这个方法就整棵子树挂不上。
  // 只补这几个，不做事件派发：测试触发事件仍然靠直接调 props 上的处理器。
  return {
    type,
    text,
    props: {},
    children: [],
    parent: null,
    value: '',
    // vModelSelect 在 mounted/updated 中读取 options；保留真实候选节点供选择同步。
    get options() {
      return walk(this, (node) => node.type === 'option')
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return true
    },
    // vModelText 的 beforeUpdate 会问「这个输入框是不是当前焦点」，用 getRootNode 反查 document
    getRootNode() {
      return fakeDocument
    }
  }
}

export function textOf(node) {
  if (!node) return ''
  // 注释节点要跳过：`v-if` 为假时 Vue 在开发模式里渲一个文本为 "v-if" 的注释占位，
  // 把它算进 innerText 会让「不渲染任何东西」的断言永远失败。
  const own = node.type === '#comment' ? '' : node.text || ''
  // `v-html` 写的是 innerHTML：对象树里看不到子节点，所以按「去标签后的可见文本」算进去，
  // 否则 Agent 的回答（走 markdown → v-html）在断言里永远是空的。
  const html = node.rawHtml ? String(node.rawHtml).replace(/<[^>]*>/g, ' ') : ''
  return [own, html, ...node.children.map(textOf)].join('')
}

function walk(node, predicate, hits = []) {
  if (predicate(node)) hits.push(node)
  for (const child of node.children) walk(child, predicate, hits)
  return hits
}

const renderer = createRenderer({
  createElement: (type) => makeNode(type),
  createText: (text) => makeNode('#text', text),
  createComment: (text) => makeNode('#comment', text),
  setText: (node, text) => {
    node.text = text
  },
  setElementText: (node, text) => {
    node.text = text
    node.children = []
  },
  insert(child, parent, anchor = null) {
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index >= 0) parent.children.splice(index, 0, child)
    else parent.children.push(child)
  },
  remove(child) {
    const parent = child.parent
    if (!parent) return
    const index = parent.children.indexOf(child)
    if (index >= 0) parent.children.splice(index, 1)
  },
  parentNode: (node) => node.parent,
  nextSibling(node) {
    const parent = node.parent
    if (!parent) return null
    return parent.children[parent.children.indexOf(node) + 1] || null
  },
  patchProp(el, key, _prev, next) {
    el.props[key] = next
    if (key === 'value') el.value = next
    // `v-html` 的记录：对象树不解析 HTML，把它单独存一份给 textOf 用
    if (key === 'innerHTML') el.rawHtml = next
  }
})

// ---------------------------------------------------------------- 环境补丁

export const downloads = []

export function installBrowserShims({ env = {}, fetchImpl = null } = {}) {
  const store = new Map()
  globalThis.__VITE_ENV__ = env
  globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    clear: () => store.clear(),
    get length() {
      return store.size
    }
  }
  globalThis.window = globalThis.window || {}
  globalThis.window.location = {
    assign: (url) => {
      globalThis.window.__redirect = url
    }
  }
  // AppSidebar 挂载时读 matchMedia 判断窄屏；测试里一律当宽屏
  globalThis.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  // saveBlob 里的延迟 revoke：测试里没有必要真的挂 60 秒的定时器（会让 node --test 迟迟不退出）
  globalThis.window.setTimeout = () => 0
  globalThis.URL.createObjectURL = () => 'blob:test'
  globalThis.URL.revokeObjectURL = () => {}
  Object.assign(fakeDocument, {
    body: { appendChild: () => {} },
    createElement: () => ({
      href: '',
      download: '',
      style: {},
      click() {
        downloads.push({ href: this.href, download: this.download })
      },
      remove() {}
    })
  })
  globalThis.document = fakeDocument
  globalThis.fetch =
    fetchImpl ||
    (async () => {
      throw new Error('测试里没有装 fetch')
    })
  return { localStorage: globalThis.localStorage, downloads }
}

export function resetShims() {
  downloads.length = 0
  globalThis.__VITE_ENV__ = {}
  if (globalThis.window) delete globalThis.window.__redirect
}

export function setSessionUser(me) {
  globalThis.localStorage.setItem('access_token', 'test-token')
  return me
}

/** 假的 `fetch`：按「方法 + 路径前缀」登记响应，命中后记录调用。 */
export function fakeFetch(routes) {
  const calls = []
  const impl = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase()
    const path = String(url).replace(/^https?:\/\/[^/]+/, '')
    calls.push({ method, path, body: options.body })
    const route = routes.find((item) => (item.method || 'GET') === method && path.startsWith(item.path))
    if (!route) throw new Error(`测试没有为 ${method} ${path} 登记响应`)
    return typeof route.response === 'function' ? route.response({ path, options }) : route.response
  }
  impl.calls = calls
  return impl
}

export function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers }
  })
}

export function blobResponse(bytes, { status = 200, headers = {} } = {}) {
  return new Response(bytes, { status, headers })
}

// ---------------------------------------------------------------- 挂载与交互

export function mount(Component, { props = {}, slots = {}, global = {} } = {}) {
  const host = defineComponent({ render: () => h(Component, props, slots) })
  const container = { ...makeNode('#root'), textContent: '' }
  const app = renderer.createApp(host)
  for (const [name, stub] of Object.entries(global.components || {})) app.component(name, stub)
  for (const plugin of global.plugins || []) app.use(plugin)
  for (const [key, value] of Object.entries(global.provide || {})) app.provide(key, value)
  app.mount(container)

  const find = (predicate) => walk(container, predicate)[0] || null
  const all = (predicate) => walk(container, predicate)
  const byText = (needle, type = null) =>
    find((node) => (type ? node.type === type : true) && textOf(node).includes(needle))
  const buttons = () => all((node) => node.type === 'button')
  const select = () => find((node) => node.type === 'select')

  return {
    container,
    app,
    find,
    all,
    byText,
    buttons,
    select,
    text: () => textOf(container),
    button: (label) => find((node) => node.type === 'button' && textOf(node).trim() === label),
    async click(nodeOrLabel) {
      const node = typeof nodeOrLabel === 'string' ? this.button(nodeOrLabel) : nodeOrLabel
      if (!node) throw new Error(`找不到按钮：${nodeOrLabel}`)
      node.props.onClick(new Event('click'))
      await nextTick()
      return node
    },
    /** 触发一个 DOM 事件：`change` 用 `{ target: { value } }`，`input` 同理。 */
    async fire(eventName, node, payload = {}) {
      const handler = node.props[`on${eventName[0].toUpperCase()}${eventName.slice(1)}`]
      if (!handler) throw new Error(`节点上没有 on${eventName} 处理器`)
      handler(payload)
      await nextTick()
    },
    async setSelectValue(value) {
      const node = select()
      if (!node) throw new Error('页面上没有 select')
      node.props.value = value
      return this.fire('change', node, { target: { value } })
    },
    async input(node, value) {
      // 两种写法都要支持：手写 `@input` 的收到的是事件对象；`v-model` 编出来的
      // `onUpdate:modelValue` 直接吃值（它内部就是 `model.value = $event`），
      // 给它套一层 `{target:{value}}` 会把对象当字符串存进 model，后端就收到一个对象。
      if (node.props.onInput) node.props.onInput({ target: { value } })
      else if (node.props['onUpdate:modelValue']) node.props['onUpdate:modelValue'](value)
      else throw new Error('输入框上没有 input 处理器')
      node.value = value
      await nextTick()
    }
  }
}

/** 简单的事件对象：组件里只用到 `type` 与 `target`。 */
function Event(type) {
  return { type, target: { value: '' }, preventDefault() {}, stopPropagation() {} }
}

export { nextTick, h, defineComponent }
