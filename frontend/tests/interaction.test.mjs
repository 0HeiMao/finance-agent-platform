/**
 * 组件交互测试：真的「点一下 / 改一下」，再看状态和视图有没有跟着变。
 *
 * 三件事只有交互测试才抓得到，纯函数测试和 SSR 渲染测试都抓不到：
 * 1. 口径切换器的选项到底有没有被填进去（`onMounted` 竞态那一类，见组件注释）；
 * 2. 导出按钮点下去请求的是什么路径、响应头里的文件名/行数有没有被用上；
 * 3. 侧边栏点一条导航会不会真的 `router.push` + 收起抽屉。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createMemoryHistory, createRouter } from 'vue-router'

import {
  blobResponse,
  fakeFetch,
  installBrowserShims,
  jsonResponse,
  mount,
  resetShims,
  textOf
} from './support/render.mjs'

const DEPARTMENTS = {
  departments: [
    { name: '018', label: '销售部', transactions: 1056 },
    { name: '097', label: '研发部', transactions: 2855 }
  ]
}

let routes = []
let fetchImpl = null

installBrowserShims({ fetchImpl: (...args) => fetchImpl(...args) })

const { activeScope, setActiveScope, withScope } = await import('../src/api/scope.js')
const { clearCurrentUser, currentUser, loadCurrentUser } = await import('../src/api/session.js')
const ScopeSwitcher = (await import('../src/components/ScopeSwitcher.vue')).default
const ExportMenu = (await import('../src/components/ExportMenu.vue')).default
const AppSidebar = (await import('../src/components/AppSidebar.vue')).default
const AgentChat = (await import('../src/components/AgentChat.vue')).default
const { downloads } = await import('./support/render.mjs')

function useRoutes(next) {
  routes = next
  fetchImpl = fakeFetch(routes)
}

function seedUser(me) {
  clearCurrentUser()
  useRoutes([{ path: '/api/v1/auth/me', response: () => jsonResponse(me) }])
  return loadCurrentUser(true)
}

async function waitFor(predicate, timeout = 2000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (predicate()) return true
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  return false
}

// ------------------------------------------------------------------ 口径切换器

test('口径切换器：只绑一个部门的账号没有可换的，整个控件不渲染', async () => {
  await seedUser({ unrestricted: false, departments: ['018'] })
  useRoutes([{ path: '/api/v1/analysis/departments', response: () => jsonResponse(DEPARTMENTS) }])
  const view = mount(ScopeSwitcher)
  await waitFor(() => true)
  assert.equal(view.text().trim(), '')
  assert.equal(view.select(), null)
})

test('口径切换器：不受限账号默认「全公司（全部部门）」，选项来自接口', async () => {
  await seedUser({ unrestricted: true, departments: [] })
  useRoutes([{ path: '/api/v1/analysis/departments', response: () => jsonResponse(DEPARTMENTS) }])
  setActiveScope('')
  const view = mount(ScopeSwitcher)
  assert.ok(await waitFor(() => view.text().includes('研发部')), `实际渲染：${view.text()}`)
  assert.match(view.text(), /查看口径/)
  assert.match(view.text(), /全公司（全部部门）/)
  assert.match(view.text(), /销售部（1056 行）/)
  assert.equal(view.select().props.value, '')
})

test('口径切换器：多部门账号看到「我绑定的 2 个部门（合计）」，切换后写进全局口径', async () => {
  await seedUser({ unrestricted: false, departments: ['018', '097'] })
  useRoutes([{ path: '/api/v1/analysis/departments', response: () => jsonResponse(DEPARTMENTS) }])
  setActiveScope('')
  const view = mount(ScopeSwitcher)
  assert.ok(await waitFor(() => view.text().includes('研发部')))
  assert.match(view.text(), /我绑定的 2 个部门（合计）/)
  assert.doesNotMatch(view.text(), /全公司（全部部门）/)

  await view.setSelectValue('097')
  assert.equal(activeScope.value, '097')
  assert.match(view.text(), /只看「部门 097」的明细/)
  // 接下来所有请求都要带上这个口径——这正是「切了没生效」类 bug 的落脚点
  assert.equal(withScope('/dashboard?period=month'), '/dashboard?period=month&department=097')
})

test('口径切换器：接口挂了就退回默认项，不留一个空下拉', async () => {
  await seedUser({ unrestricted: true, departments: [] })
  useRoutes([
    {
      path: '/api/v1/analysis/departments',
      response: () => jsonResponse({ detail: '炸了' }, { status: 500 })
    }
  ])
  setActiveScope('')
  const view = mount(ScopeSwitcher)
  assert.ok(await waitFor(() => view.text().includes('查看口径')))
  assert.match(view.text(), /全公司（全部部门）/)
  assert.doesNotMatch(view.text(), /研发部/)
})

// ------------------------------------------------------------------ 导出按钮组

test('导出按钮组：点 CSV 用响应头里的文件名与行数，并把口径拼进请求', async () => {
  setActiveScope('018')
  useRoutes([
    {
      path: '/api/v1/export/transactions',
      response: () =>
        blobResponse('a,b\n1,2\n', {
          headers: {
            'Content-Disposition':
              'attachment; filename="transactions-2026-10.csv"; filename*=UTF-8\'\'%E6%98%8E%E7%BB%86-2026-10.csv',
            'X-Export-Rows': '120',
            'X-Export-Truncated': 'true'
          }
        })
    }
  ])
  downloads.length = 0
  const view = mount(ExportMenu, { props: { kind: 'transactions', period: '2026-10' } })
  await view.click('导出 CSV')
  await waitFor(() => view.text().includes('已保存'))

  const request = fetchImpl.calls.at(-1)
  assert.match(request.path, /^\/api\/v1\/export\/transactions\?format=csv&period=2026-10&department=018$/)
  // 中文文件名走 filename*（HTTP 头只能 latin-1），ASCII 兜底名是给老浏览器的
  assert.equal(downloads.at(-1).download, '明细-2026-10.csv')
  assert.match(view.text(), /已保存 明细-2026-10\.csv（120 行，已达上限被截断，\d+ B）/)
  setActiveScope('')
})

test('导出按钮组：没有 X-Export-Rows 时不编造行数；失败时给出后端那句话', async () => {
  useRoutes([
    {
      path: '/api/v1/export/metrics',
      response: () =>
        blobResponse('x'.repeat(2048), {
          headers: { 'Content-Disposition': 'attachment; filename="metrics.xlsx"' }
        })
    }
  ])
  const ok = mount(ExportMenu, { props: { kind: 'metrics', period: '2026-10' } })
  await ok.click('导出 Excel')
  await waitFor(() => ok.text().includes('已保存'))
  assert.match(ok.text(), /已保存 metrics\.xlsx（2 KB）/)
  assert.doesNotMatch(ok.text(), /行/)

  useRoutes([
    {
      path: '/api/v1/export/alerts',
      response: () => jsonResponse({ detail: '导出太频繁，请稍后再试' }, { status: 429 })
    }
  ])
  const failed = mount(ExportMenu, { props: { kind: 'alerts' } })
  await failed.click('导出 CSV')
  await waitFor(() => failed.text().includes('导出失败'))
  assert.match(failed.text(), /导出失败：导出太频繁，请稍后再试/)
  const message = failed.find((node) => String(node.props.class || '').includes('export-msg'))
  assert.match(String(message.props.class), /bad/) // 失败要变红，不是成功那行的绿色
})

// ------------------------------------------------------------------ 侧边栏

function makeRouter() {
  const blank = { render: () => null }
  return createRouter({
    history: createMemoryHistory(),
    routes: ['/dashboard', '/agent', '/financial/transactions', '/admin'].map((path) => ({
      path,
      component: blank
    }))
  })
}

test('侧边栏：不受限账号看到「管理员」组，受限账号看不到', async () => {
  await seedUser({ unrestricted: true, departments: [] })
  const adminRouter = makeRouter()
  adminRouter.push('/dashboard')
  await adminRouter.isReady()
  const admin = mount(AppSidebar, { props: { open: false }, global: { plugins: [adminRouter] } })
  assert.ok(await waitFor(() => admin.text().includes('管理员')))
  assert.match(admin.text(), /管理概览/)
  assert.match(admin.text(), /明细更正/) // ⑨ 新入口必须在导航里
  assert.match(admin.text(), /财务分析 Agent/)

  await seedUser({ unrestricted: false, departments: ['018'] })
  const scopedRouter = makeRouter()
  scopedRouter.push('/dashboard')
  await scopedRouter.isReady()
  const scoped = mount(AppSidebar, { props: { open: false }, global: { plugins: [scopedRouter] } })
  await waitFor(() => scoped.text().includes('看板概览'))
  assert.doesNotMatch(scoped.text(), /管理员/)
  assert.doesNotMatch(scoped.text(), /管理概览/)
})

test('侧边栏：点导航会 router.push 并申请收起抽屉；当前页高亮', async () => {
  await seedUser({ unrestricted: true, departments: [] })
  const router = makeRouter()
  router.push('/dashboard')
  await router.isReady()
  let closed = 0
  const view = mount(AppSidebar, {
    props: {
      open: true,
      onRequestClose: () => {
        closed += 1
      }
    },
    global: { plugins: [router] }
  })
  const navItem = (label) =>
    view.find((node) => String(node.props.class || '').includes('nav-item') && textOf(node).includes(label))

  assert.match(String(navItem('看板概览').props.class), /active/)
  assert.doesNotMatch(String(navItem('明细更正').props.class), /active/)

  await view.click(navItem('明细更正'))
  // router.push 是异步的：等路由真的换了再断言高亮，否则测的是「点了没反应」的假象
  assert.ok(await waitFor(() => router.currentRoute.value.path === '/financial/transactions'))
  assert.equal(closed, 1)
  assert.match(String(navItem('明细更正').props.class), /active/)
  assert.doesNotMatch(String(navItem('看板概览').props.class), /active/)
})

test('侧边栏：当前路由已经是目标页时不重复 push（点当前页只收抽屉）', async () => {
  await seedUser({ unrestricted: true, departments: [] })
  const router = makeRouter()
  router.push('/dashboard')
  await router.isReady()
  const pushed = []
  const original = router.push.bind(router)
  router.push = (to) => {
    pushed.push(to)
    return original(to)
  }
  const view = mount(AppSidebar, {
    props: { open: true, onRequestClose: () => {} },
    global: { plugins: [router] }
  })
  const navItem = view.find(
    (node) => String(node.props.class || '').includes('nav-item') && textOf(node).includes('看板概览')
  )
  await view.click(navItem)
  assert.deepEqual(pushed, [])
})

// ------------------------------------------------------------------ Agent 对话框

function sseResponse(events) {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    start(controller) {
      for (const event of events) controller.enqueue(encoder.encode(event))
      controller.close()
    }
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

test('Agent 对话框：点示例问题走流式回答，依据按类型打标并去重', async () => {
  setActiveScope('097')
  useRoutes([
    {
      method: 'POST',
      path: '/api/v1/ai/agent/chat/stream',
      response: () =>
        sseResponse([
          'event: delta\ndata: {"text":"本月"}\n\n',
          'event: delta\ndata: {"text":"收支差额为 -5,357,304,117,380.05。"}\n\n',
          'event: done\ndata: {"sources":[{"name":"财务汇总表 2026-07","kind":"financial_metric"},' +
            '{"name":"成本表.xlsx","kind":"knowledge_base"},{"name":"成本表.xlsx","kind":"knowledge_base"},' +
            '{"name":"明细账 2026-07","kind":"financial_transaction"}]}\n\n'
        ])
    }
  ])
  const view = mount(AgentChat, {
    props: { full: true, examples: ['本月收支差额是多少？'] },
    global: { components: { 'router-link': { render: () => null } } }
  })
  assert.match(view.text(), /试试这些问题/)
  assert.match(view.text(), /本月收支差额是多少？/)

  await view.click('本月收支差额是多少？')
  assert.ok(await waitFor(() => view.text().includes('依据：')), `实际渲染：${view.text()}`)
  const request = fetchImpl.calls.at(-1)
  assert.equal(request.path, '/api/v1/ai/agent/chat/stream?department=097')
  assert.deepEqual(JSON.parse(request.body), { question: '本月收支差额是多少？' })
  assert.match(view.text(), /收支差额为 -5,357,304,117,380\.05/)
  // 后端给财务证据的名字本身以类型开头，不能再补前缀；知识库片段才补「知识库·」；同一文件去重
  assert.match(view.text(), /依据：财务汇总表 2026-07、知识库·成本表\.xlsx、明细账 2026-07/)
  assert.doesNotMatch(view.text(), /财务汇总表·/)
  assert.doesNotMatch(view.text(), /成本表\.xlsx、知识库·成本表/)
  setActiveScope('')
})

test('Agent 对话框：流断掉时显示错误并可重试，清空会话只在有消息后出现', async () => {
  useRoutes([
    {
      method: 'POST',
      path: '/api/v1/ai/agent/chat/stream',
      response: () => jsonResponse({ detail: '模型暂时不可用' }, { status: 503 })
    }
  ])
  const view = mount(AgentChat, {
    props: { full: true },
    global: { components: { 'router-link': { render: () => null } } }
  })
  assert.equal(view.button('清空会话'), null)

  const input = view.find((node) => node.type === 'input')
  // 问题用一句「不会出现在空态提示与输入框 placeholder 里」的话，
  // 否则最后那条「清空后提问要消失」的断言会被占位文案里的同名句子骗过
  await view.input(input, '差旅费为什么超了？')
  await view.click('发送')
  assert.ok(await waitFor(() => view.text().includes('模型暂时不可用')), `实际渲染：${view.text()}`)
  assert.match(view.text(), /差旅费为什么超了？/) // 提问本身要留在消息流里
  assert.equal(view.button('重试') === null, false)

  // 重试用的是上一条问题（输入框早被清空了），并且这次也失败：等错误第二次出现，
  // 说明这一轮请求真的落地了，而不是「按钮还在上一次的渲染里」
  await view.click('重试')
  assert.ok(await waitFor(() => fetchImpl.calls.length > 1))
  assert.ok(await waitFor(() => view.button('重试') !== null), `实际渲染：${view.text()}`)

  // 换成成功的流再重试一次：这次有回答，于是出现「清空会话」，点掉后回到空态
  useRoutes([
    {
      method: 'POST',
      path: '/api/v1/ai/agent/chat/stream',
      response: () => sseResponse(['event: done\ndata: {"sources":[]}\n\n'])
    }
  ])
  await view.click('重试')
  assert.ok(await waitFor(() => view.text().includes('清空会话')), `实际渲染：${view.text()}`)
  await view.click('清空会话')
  assert.doesNotMatch(view.text(), /差旅费为什么超了？/)
  assert.match(view.text(), /可以问我/)
  assert.equal(view.button('清空会话'), null)
})

// ------------------------------------------------------------------ 后台任务（⑩）

const BATCH_ROW = {
  batch: 'b'.repeat(64),
  short: 'bbbbbbbbbbbb',
  rows: 12,
  first_date: '2026-01-01',
  last_date: '2026-03-31',
  periods: ['2026-01', '2026-02', '2026-03'],
  imported_at: '2026-10-08 12:00:00'
}

function jobFixture(overrides = {}) {
  return {
    id: 77,
    kind: 'batch.revert',
    kind_label: '撤销导入批次',
    status: 'failed',
    actor: 'demo_analyst',
    created_at: '2026-10-08T16:23:17',
    started_at: '2026-10-08T16:23:18',
    finished_at: '2026-10-08T16:23:19',
    attempts: 3,
    max_attempts: 3,
    next_attempt_at: null,
    error: '第 3 次尝试失败：批次不存在或已被撤销',
    result: null,
    ...overrides
  }
}

function batchesRoutes(extra = []) {
  return [
    {
      path: '/api/v1/auth/me',
      response: () => jsonResponse({ username: 'demo_analyst', unrestricted: true, departments: [] })
    },
    ...extra,
    { path: '/api/v1/admin/batches', response: () => jsonResponse({ data: [BATCH_ROW], total: 1 }) },
    { path: '/api/v1/admin/jobs', response: () => jsonResponse({ data: [jobFixture()], total: 1 }) },
    { path: '/api/v1/admin/jobs/77', response: () => jsonResponse(jobFixture({ status: 'queued' })) },
    { path: '/api/v1/admin/jobs/91', response: () => jsonResponse(jobFixture({ id: 91, status: 'running' })) }
  ]
}

async function mountBatches(routes) {
  useRoutes(routes)
  const router = makeRouter()
  router.push('/admin')
  await router.isReady()
  const AdminBatches = (await import('../src/views/AdminBatches.vue')).default
  const view = mount(AdminBatches, { global: { plugins: [router] } })
  assert.ok(await waitFor(() => view.text().includes('#77')), `实际渲染：${view.text()}`)
  return view
}

test('后台任务（⑩）：失败的任务显示「第 3/3 次」并能点「重试」，请求打到 /retry', async () => {
  const view = await mountBatches(
    batchesRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/jobs/77/retry',
        response: () =>
          jsonResponse(jobFixture({ status: 'queued', attempts: 3, max_attempts: 6, next_attempt_at: null }))
      }
    ])
  )
  // 失败不再等于「没救了」：界面必须看出已经用掉几次预算，否则人工重试无从判断
  assert.match(view.text(), /第 3\/3 次/)
  assert.match(view.text(), /第 3 次尝试失败：批次不存在或已被撤销/)

  await view.click('重试')
  assert.ok(
    fetchImpl.calls.some((call) => call.method === 'POST' && call.path === '/api/v1/admin/jobs/77/retry'),
    JSON.stringify(fetchImpl.calls)
  )
  // 点下去只是发出请求：提示要等响应回来才出现（所以要等，而不是立刻断言）
  assert.ok(await waitFor(() => view.text().includes('已重新排队')), `实际渲染：${view.text()}`)
  assert.match(view.text(), /已重新排队（第 4\/6 次尝试）/)
  view.app.unmount() // 轮询定时器要停掉，否则 node --test 不退出
})

test('后台任务（⑩）：排队等退避时显示「第 N/M 次」，重刷到的成功结果说明「此前已被撤销」', async () => {
  const view = await mountBatches(
    batchesRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/jobs/77/retry',
        response: () => jsonResponse(jobFixture({ status: 'queued', attempts: 3, max_attempts: 6 }))
      },
      {
        path: '/api/v1/admin/jobs/77',
        response: () =>
          jsonResponse(
            jobFixture({
              status: 'succeeded',
              attempts: 2,
              max_attempts: 3,
              started_at: '2026-10-08T16:23:18',
              finished_at: '2026-10-08T16:23:21',
              error: null,
              result: { deleted_rows: 0, recalculated: [], cleared: [], already_reverted: true }
            })
          )
      }
    ])
  )
  // 首屏读到的是失败那条（mock 的 GET /admin/jobs 列表），点重试后轮询到的是 succeeded
  assert.match(view.text(), /第 3\/3 次/)
  await view.click('重试')
  assert.ok(await waitFor(() => view.text().includes('已经被撤销过')), `实际渲染：${view.text()}`)
  view.app.unmount()
})

test('后台任务（⑩）：同一批次重复提交被后端去重时，提示「没有重复提交」而不是「已提交」', async () => {
  const view = await mountBatches(
    batchesRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/jobs/batch-revert',
        response: () => jsonResponse({ ...jobFixture({ id: 91, status: 'running' }), deduplicated: true })
      }
    ])
  )
  await view.click('撤销批次')
  await view.click('确认撤销')
  assert.ok(await waitFor(() => view.text().includes('已经在队列里')), `实际渲染：${view.text()}`)
  assert.doesNotMatch(view.text(), /已提交后台任务/)
  view.app.unmount()
})

// ------------------------------------------------------------ 预警外发（⑭）

const CHANNEL_ROW = {
  id: 5,
  name: '财务群机器人',
  kind: 'webhook',
  kind_label: 'Webhook（HTTP POST）',
  target: 'http://gateway.internal/hook',
  min_level: 'low',
  min_level_label: '低及以上（全部预警）',
  department: '',
  enabled: true,
  has_secret: true,
  last_result: null,
  last_error: null,
  last_sent_at: null,
  created_at: '2026-10-08T16:00:00',
  updated_at: '2026-10-08T16:00:00',
  counts: { pending: 1, sent: 2, failed: 0 }
}

const DELIVERY_ROW = {
  id: 41,
  alert_key: '2026-10||成本率异常|cost_ratio',
  alert_id: 1482,
  channel_id: 5,
  channel_name: '财务群机器人',
  channel_kind: 'webhook',
  channel_kind_label: 'Webhook（HTTP POST）',
  period: '2026-10',
  department: '',
  alert_type: '成本率异常',
  metric_name: 'cost_ratio',
  level: 'high',
  level_label: '高',
  status: 'failed',
  status_label: '已失败',
  attempts: 3,
  response_status: 500,
  last_error: 'HTTP 500：网关忙',
  sent_at: null,
  created_at: '2026-10-08T17:02:11',
  updated_at: '2026-10-08T17:03:20'
}

function channelsRoutes(extra = []) {
  return [
    {
      path: '/api/v1/auth/me',
      response: () => jsonResponse({ username: 'demo_analyst', unrestricted: true, departments: [] })
    },
    ...extra,
    {
      path: '/api/v1/admin/alert-channels',
      response: () =>
        jsonResponse({
          data: [CHANNEL_ROW],
          kinds: { webhook: 'Webhook（HTTP POST）', email: '邮件（SMTP）' },
          min_levels: { low: '低及以上（全部预警）', medium: '中及以上', high: '仅高级预警' },
          levels: { high: '高', medium: '中', low: '低' },
          departments: ['018', '097'],
          stats: { pending: 1, sent: 2, failed: 0 }
        })
    },
    {
      path: '/api/v1/admin/alert-deliveries',
      response: () =>
        jsonResponse({
          data: [DELIVERY_ROW],
          total: 1,
          limit: 50,
          offset: 0,
          status_labels: { pending: '待发送', sent: '已送达', failed: '已失败' },
          levels: { high: '高', medium: '中', low: '低' },
          stats: { pending: 1, sent: 2, failed: 0 }
        })
    }
  ]
}

async function mountChannels(routes) {
  useRoutes(routes)
  const router = makeRouter()
  router.push('/admin')
  await router.isReady()
  const AdminAlertChannels = (await import('../src/views/AdminAlertChannels.vue')).default
  const view = mount(AdminAlertChannels, { global: { plugins: [router] } })
  assert.ok(await waitFor(() => view.text().includes('财务群机器人')), `实际渲染：${view.text()}`)
  return view
}

/** 台账筛选里的「状态」下拉：只有它的选项里会出现 `pending` 这个值。 */
function statusFilter(view) {
  return view
    .all((node) => node.type === 'select')
    .find((node) =>
      (node.children || []).some((child) => child.type === 'option' && child.props?.value === 'pending')
    )
}

test('预警外发（⑭）：通道计数与投递台账都在，点「测试」把失败原因写回「最近结果」', async () => {
  const view = await mountChannels(
    channelsRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/alert-channels/5/test',
        response: () =>
          jsonResponse({
            ok: false,
            status: 500,
            detail: 'HTTP 500：网关忙',
            duration_ms: 12,
            channel: {
              ...CHANNEL_ROW,
              last_result: 'failed',
              last_error: 'HTTP 500：网关忙',
              last_sent_at: '2026-10-08T18:00:00'
            }
          })
      }
    ])
  )
  // 台账那一列的三个数字是管理员判断「是不是堵住了」的唯一依据
  assert.match(view.text(), /待发 1/)
  assert.match(view.text(), /已送达 2/)
  assert.match(view.text(), /全公司 · cost_ratio/)
  assert.match(view.text(), /已失败/)
  assert.match(view.text(), /HTTP 500：网关忙/) // 台账里的「失败原因」
  assert.match(view.text(), /还没发过/)

  await view.click('测试')
  assert.ok(
    fetchImpl.calls.some(
      (call) => call.method === 'POST' && call.path === '/api/v1/admin/alert-channels/5/test'
    ),
    JSON.stringify(fetchImpl.calls)
  )
  // 测试失败也回 200：界面要按 `ok` 判断，把「发不出去」的原因摆出来，而不是当请求失败
  assert.ok(
    await waitFor(() => view.text().includes('发不出去：HTTP 500：网关忙')),
    `实际渲染：${view.text()}`
  )
  assert.doesNotMatch(view.text(), /还没发过/)
  assert.match(view.text(), /已配签名密钥/)
  assert.doesNotMatch(view.text(), /outbound-verify/) // 密钥只写不读，界面上不会出现它
  view.app.unmount()
})

test('预警外发（⑭）：点「补发通知」把待处理预警补成投递单，并重新读一次台账', async () => {
  const view = await mountChannels(
    channelsRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/alerts/notify',
        response: () =>
          jsonResponse({ period: '', created: 3, queued: 3, job: { id: 9 }, deduplicated: false })
      }
    ])
  )
  const readsBefore = fetchImpl.calls.filter((call) =>
    call.path.startsWith('/api/v1/admin/alert-deliveries')
  ).length

  await view.click('补发通知')
  const notifyCall = fetchImpl.calls.find((call) => call.path === '/api/v1/admin/alerts/notify')
  assert.ok(notifyCall, JSON.stringify(fetchImpl.calls))
  assert.equal(notifyCall.method, 'POST')
  // 期间留空 = 全部：补的是「已存在的待处理预警」，不是「按期间重算」
  assert.deepEqual(JSON.parse(notifyCall.body), { period: '' })
  assert.ok(
    await waitFor(() => view.text().includes('新建 3 条投递，其中 3 条已排队')),
    `实际渲染：${view.text()}`
  )
  assert.ok(
    fetchImpl.calls.filter((call) => call.path.startsWith('/api/v1/admin/alert-deliveries')).length >
      readsBefore,
    '补发之后要重新读台账，否则看不到新建的投递单'
  )
  view.app.unmount()
})

test('预警外发（⑭）：台账筛选带 status 查询参数，点「重发」打到 /retry 并显示排队', async () => {
  const view = await mountChannels(
    channelsRoutes([
      {
        method: 'POST',
        path: '/api/v1/admin/alert-deliveries/41/retry',
        response: () =>
          jsonResponse({
            data: {
              ...DELIVERY_ROW,
              status: 'pending',
              status_label: '待发送',
              attempts: 1,
              last_error: null
            },
            job: { id: 12 },
            queued: true,
            deduplicated: false
          })
      }
    ])
  )

  const node = statusFilter(view)
  assert.ok(node, `找不到状态筛选下拉：${view.text()}`)
  await view.fire('change', node, { target: { value: 'failed' } })
  await view.click('筛选')
  assert.ok(
    fetchImpl.calls.some(
      (call) => call.method === 'GET' && call.path === '/api/v1/admin/alert-deliveries?status=failed&limit=50'
    ),
    JSON.stringify(fetchImpl.calls)
  )

  await view.click('重发')
  assert.ok(
    fetchImpl.calls.some(
      (call) => call.method === 'POST' && call.path === '/api/v1/admin/alert-deliveries/41/retry'
    ),
    JSON.stringify(fetchImpl.calls)
  )
  assert.ok(await waitFor(() => view.text().includes('已重新排队，稍后自动重发')), `实际渲染：${view.text()}`)
  view.app.unmount()
})

test.after(() => {
  resetShims()
  clearCurrentUser()
  setActiveScope('')
  assert.ok(currentUser())
})
