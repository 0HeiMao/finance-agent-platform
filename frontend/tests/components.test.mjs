/**
 * 组件渲染测试：不查数据、只查「给什么 props 就渲染成什么」。
 *
 * 跑法：`npm test`（`node --test --import ./tests/support/register.mjs tests/*.test.mjs`）。
 * `.vue` 由 tests/support/sfc-loader.mjs 现场编译，挂载用无 DOM 的自定义渲染器，
 * 所以这条用例不需要浏览器、也不需要后端。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { installBrowserShims, mount, resetShims } from './support/render.mjs'
import { renderMarkdown } from '../src/utils/markdown.js'

installBrowserShims()

const PagePager = (await import('../src/components/PagePager.vue')).default
const DataScopeNotice = (await import('../src/components/DataScopeNotice.vue')).default

test('PagePager 给出「第 x / y 页」与当前区间，并禁用第一页的上一页', () => {
  const view = mount(PagePager, { props: { total: 200, limit: 50, offset: 0, unit: '条明细' } })
  const text = view.text()
  assert.match(text, /第 1 \/ 4 页/)
  assert.match(text, /当前 1–50 条明细 \/ 共 200 条明细/)
  assert.equal(view.button('上一页').props.disabled, true)
  assert.equal(view.button('下一页').props.disabled, false)
})

test('PagePager 点「下一页」按 limit 步进，点到最后一页就停下', async () => {
  const emitted = []
  const view = mount(PagePager, {
    props: {
      total: 120,
      limit: 50,
      offset: 0,
      onChange: (value) => emitted.push(value)
    }
  })
  // 组件是「props 驱动」的：父组件把新 offset 传回来才算翻页，所以这里手工模拟父组件。
  await view.click('下一页')
  assert.deepEqual(emitted, [50])
  assert.match(view.text(), /第 1 \/ 3 页/) // props 没变，视图不该自作主张翻页

  const second = mount(PagePager, {
    props: { total: 120, limit: 50, offset: 50, onChange: (value) => emitted.push(value) }
  })
  assert.match(second.text(), /第 2 \/ 3 页/)
  await second.click('下一页')
  assert.deepEqual(emitted, [50, 100])

  // 最后一页：按钮禁用，且组件自己的守卫（100 + 50 >= 120）不会再发事件——
  // 这里直接调处理器，等于绕过浏览器的 disabled，能验到守卫本身
  const last = mount(PagePager, {
    props: { total: 120, limit: 50, offset: 100, onChange: (value) => emitted.push(value) }
  })
  assert.equal(last.button('下一页').props.disabled, true)
  assert.equal(last.button('上一页').props.disabled, false)
  assert.match(last.text(), /当前 101–120 条 \/ 共 120 条/)
  await last.click('下一页')
  assert.deepEqual(emitted, [50, 100])
})

test('PagePager 没有数据时显示 0 条且两个按钮都禁用（不能拿当前页条数冒充总数）', () => {
  const view = mount(PagePager, { props: { total: 0, limit: 50, offset: 0 } })
  assert.match(view.text(), /第 1 \/ 1 页/)
  assert.doesNotMatch(view.text(), /当前/)
  assert.equal(view.button('上一页').props.disabled, true)
  assert.equal(view.button('下一页').props.disabled, true)
})

test('PagePager 加载中禁用翻页（避免连点连发请求）', () => {
  const view = mount(PagePager, { props: { total: 200, limit: 50, offset: 50, loading: true } })
  assert.equal(view.button('上一页').props.disabled, true)
  assert.equal(view.button('下一页').props.disabled, true)
})

test('DataScopeNotice 只在受限口径下出现，且用后端的 label 与 note', () => {
  const hidden = mount(DataScopeNotice, {
    props: { scope: { unrestricted: true, label: '全公司（全部部门）' } }
  })
  assert.equal(hidden.text().trim(), '')

  const empty = mount(DataScopeNotice, { props: {} })
  assert.equal(empty.text().trim(), '')

  const restricted = mount(DataScopeNotice, {
    props: { scope: { unrestricted: false, label: '部门 018', note: '只统计 018 的明细' } }
  })
  assert.match(restricted.text(), /数据范围：部门 018/)
  assert.match(restricted.text(), /只统计 018 的明细/)

  // 后端没给 note 时要落在兜底文案上，而不是渲染出「undefined」
  const fallback = mount(DataScopeNotice, { props: { scope: { unrestricted: false, label: '部门 097' } } })
  assert.match(fallback.text(), /当前账号只统计本部门明细/)
  assert.doesNotMatch(fallback.text(), /undefined/)
})

test('markdown 渲染与组件一起可用（纯净函数，先转义再解析）', () => {
  // 组件测里顺带把 markdown 的转义边界再钉一次：它喂给 v-html，出错就是 XSS
  const html = renderMarkdown('## 标题\n\n<script>alert(1)</script>\n\n- a\n- b')
  assert.match(html, /<h3>标题<\/h3>/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /&lt;script&gt;/)
})

test.after(() => resetShims())
