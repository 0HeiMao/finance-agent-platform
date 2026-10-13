<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getDashboard } from '../api/dashboard'
import AppSidebar from '../components/AppSidebar.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import ExportMenu from '../components/ExportMenu.vue'
import { activeScope } from '../api/scope'

const router = useRouter()
const route = useRoute()
const sidebarOpen = ref(false)
const period = ref('year')
const data = ref(null)
const loading = ref(false)
const error = ref('')
const updatedAt = ref('')
let request = null

const kpis = computed(() => (data.value && data.value.kpi) || [])
const alerts = computed(() => ((data.value && data.value.alerts) || []).slice(0, 6))
const alertTotal = computed(() => ((data.value && data.value.alerts) || []).length)
const notes = computed(() => (data.value && data.value.chart_notes) || {})
const hasData = computed(() => Boolean(data.value && data.value.has_data))
const compare = computed(() => data.value && data.value.compare)

// 后 6 个月做趋势图（7 期数据时前 6 期是全额、最后一期只有 93 行，曲线会严重误导）
const tail = computed(() => {
  const d = data.value || { months: [], revenue: [] }
  return { months: d.months.slice(-6), revenue: d.revenue.slice(-6) }
})
const updatedText = computed(() => (updatedAt.value ? `数据更新于 ${updatedAt.value}` : '尚未取到数据'))
// 图表只画最近 6 个月，说明文字也跟着收窄，避免出现图上没有的月份
const revenueNote = computed(() => {
  const months = tail.value.months.map((m) => m.slice(5)).join('、')
  return months ? `按交易日期逐月汇总（${months}），单位为${scaleUnit.value.name}` : notes.value.revenue
})

const revenueEl = ref(null)
const expenseEl = ref(null)
const cashEl = ref(null)
let charts = []
let observers = []
const scaleUnit = ref({ name: '万元', divisor: 1e4 })

// 金额列用紧凑单位，避免 20 位大数把卡片撑破；完整数值放在 title 里可悬停核对
const AMOUNT_UNITS = [
  ['亿元', 1e8],
  ['万元', 1e4],
  ['元', 1]
]
function brief(kpi) {
  const full = `${kpi.value} ${kpi.unit}`
  if (kpi.unit !== '元') return { value: kpi.value, unit: kpi.unit, full }
  const raw = Number(String(kpi.value).replace(/,/g, ''))
  if (!Number.isFinite(raw)) return { value: kpi.value, unit: kpi.unit, full }
  const [unit, scale] = AMOUNT_UNITS.find(([, s]) => Math.abs(raw) >= s) || AMOUNT_UNITS[2]
  const scaled = raw / scale
  // 亿元/万元保留 2 位小数（5.88 万亿 → 58,863.21 亿元），否则单张卡片放不下 20 位数字；
  // 精确到分的原始金额仍在 title 里，悬停即可核对。
  return {
    value: scaled.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    unit,
    full
  }
}
function humanTime(value) {
  if (!value) return '—'
  const text = String(value).replace('T', ' ')
  return text.length >= 16 ? text.slice(0, 16) : text
}
function fmtTime(value) {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false })
}

function renderCharts() {
  disposeCharts()
  if (!hasData.value) return
  const axis = { axisLine: { lineStyle: { color: '#dce5ee' } }, axisLabel: { color: '#718096' } }
  const tooltip = { trigger: 'axis', backgroundColor: '#173b5d', textStyle: { color: '#fff' } }
  // 亿元量级用亿元刻度，否则 0 和 1 亿之间的曲线看不出差别
  const top = Math.max(...(data.value.revenue || [0]), ...(data.value.cash.inflow || [0]))
  scaleUnit.value = Math.abs(top) >= 1e8 ? { name: '亿元', divisor: 1e8 } : { name: '万元', divisor: 1e4 }
  const scaled = (v) => Number((Number(v || 0) / scaleUnit.value.divisor).toFixed(4))
  const yAxis = {
    type: 'value',
    name: scaleUnit.value.name,
    nameTextStyle: { color: '#9aa9bb', fontSize: 11 },
    splitLine: { lineStyle: { color: '#eef2f6' } },
    axisLabel: axis.axisLabel
  }

  const r = echarts.init(revenueEl.value)
  r.setOption({
    color: ['#397fb5'],
    tooltip: { ...tooltip, valueFormatter: (v) => `${v} ${scaleUnit.value.name}` },
    grid: { left: 12, right: 32, top: 30, bottom: 12, containLabel: true },
    xAxis: {
      type: 'category',
      data: tail.value.months,
      boundaryGap: false,
      axisLine: axis.axisLine,
      axisLabel: axis.axisLabel
    },
    yAxis,
    series: [
      {
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        data: tail.value.revenue.map(scaled),
        areaStyle: { color: 'rgba(57,127,181,.10)' },
        lineStyle: { width: 3 }
      }
    ]
  })

  const e = echarts.init(expenseEl.value)
  const expenses = data.value.expenses || []
  e.setOption({
    color: ['#6186dd'],
    tooltip: {
      trigger: 'axis',
      renderMode: 'richText',
      valueFormatter: (v) => `${v} ${scaleUnit.value.name}`
    },
    grid: { left: 12, right: 20, top: 28, bottom: 12, containLabel: true },
    xAxis: { ...yAxis, name: '' },
    yAxis: {
      type: 'category',
      inverse: true,
      data: expenses.map((item) => item.name),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#718096', width: 80, overflow: 'truncate' }
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 18,
        itemStyle: { borderRadius: 4 },
        data: expenses.map((item) => scaled(item.value))
      }
    ]
  })

  const c = data.value.cash.available === false ? null : echarts.init(cashEl.value)
  c?.setOption({
    color: ['#52a77b', '#e98989'],
    tooltip: { ...tooltip, valueFormatter: (v) => `${v} ${scaleUnit.value.name}` },
    legend: { top: 22, right: 0, textStyle: { color: '#718096', fontSize: 11 } },
    grid: { left: 62, right: 16, top: 58, bottom: 30 },
    xAxis: { type: 'category', data: data.value.cash.x, axisLine: axis.axisLine, axisLabel: axis.axisLabel },
    yAxis,
    series: [
      { name: '流入', type: 'bar', barWidth: 18, data: data.value.cash.inflow.map(scaled) },
      { name: '流出', type: 'bar', barWidth: 18, data: data.value.cash.outflow.map(scaled) }
    ]
  })
  charts = [r, e, c].filter(Boolean)
  observers = [revenueEl, expenseEl, cashEl]
    .filter((el) => el.value)
    .map((el) => {
      const ob = new ResizeObserver(() => charts.forEach((chart) => chart.resize()))
      ob.observe(el.value)
      return ob
    })
}
function disposeCharts() {
  observers.forEach((ob) => ob.disconnect())
  observers = []
  charts.forEach((c) => c.dispose())
  charts = []
}

async function loadData() {
  request?.abort()
  const current = new AbortController()
  request = current
  loading.value = true
  error.value = ''
  try {
    const payload = await getDashboard(period.value, current.signal)
    if (current.signal.aborted) return
    data.value = payload || null
    updatedAt.value = payload && payload.updated_at ? fmtTime(payload.updated_at) : ''
  } catch (err) {
    if (current.signal.aborted) return
    data.value = null
    updatedAt.value = ''
    error.value = err && err.message ? err.message : '加载失败，请稍后重试'
  } finally {
    if (request === current) loading.value = false
  }
  await nextTick()
  if (current.signal.aborted) return
  renderCharts()
}
function refresh() {
  if (!loading.value) loadData()
}

watch(period, loadData)
// 口径切换后必须重新取数：看板的每个数字都依赖口径，留着旧数据会出现「标签说部门、数字是全公司」。
watch(activeScope, loadData)
onMounted(loadData)
onBeforeUnmount(() => {
  request?.abort()
  disposeCharts()
})

// 预警卡显示的是「本期范围」内的全部预警；导出要用同一范围，所以给的是区间而不是单个期间，
// 用 `data.period`（值是 month/quarter/year，那是期间类型不是期间）会被后端按格式挡回 422。
const alertRange = computed(() => {
  const months = (data.value && data.value.months) || []
  return { from: months[0] || '', to: months[months.length - 1] || '' }
})
</script>

<template>
  <div class="app dashboard-page">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>财务看板概览</h1>
            <p>
              <span data-visual-mask>{{ updatedText }}</span>
              <span v-if="data && data.range"> · 期间 {{ data.range.label }}</span>
            </p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
          <select v-model="period" class="select">
            <option value="month">本月</option>
            <option value="quarter">本季</option>
            <option value="year">本年</option>
          </select>
          <button class="btn" :disabled="loading" @click="refresh">
            {{ loading ? '加载中…' : '↻ 刷新数据' }}
          </button>
        </div>
      </header>

      <section class="overview-intro">
        <div>
          <span class="eyebrow">财务工作空间 / OVERVIEW</span>
          <h2>看清经营变化，找到下一步。</h2>
          <p>从核心指标到异常明细，让每一次分析都有数据依据。</p>
        </div>
        <div class="overview-links">
          <router-link class="btn" to="/financial/import">导入数据</router-link
          ><router-link class="btn primary" to="/agent">✦ 开始 AI 分析 →</router-link>
        </div>
      </section>

      <section v-if="route.query.denied === 'admin'" class="state-box notice">
        <strong>管理入口不可访问</strong>
        <p>当前账号没有访问此管理入口的权限，请联系管理员核对账号权限。</p>
      </section>

      <DataScopeNotice :scope="data && data.scope" />
      <details v-if="data?.data_quality?.length" class="data-quality card">
        <summary>数据口径与完整性 · {{ data.data_quality.length }} 项说明</summary>
        <ul>
          <li v-for="item in data.data_quality" :key="item.code">{{ item.message }}</li>
        </ul>
      </details>

      <section v-if="error" class="state-box error-state">
        <strong>数据加载失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="refresh">重试</button>
      </section>

      <template v-else>
        <section v-if="loading && !kpis.length" class="kpi-grid">
          <article v-for="n in 6" :key="n" class="card kpi skeleton">
            <div class="sk-line w60" />
            <div class="sk-line w80 tall" />
            <div class="sk-line w40" />
          </article>
        </section>

        <section v-else-if="!hasData" class="state-box empty-state">
          <strong>暂无财务数据</strong>
          <p>{{ (data && data.message) || '请先导入财务数据后查看看板。' }}</p>
          <button class="btn" @click="router.push('/financial/import')">去导入数据</button>
        </section>

        <template v-else>
          <section v-if="compare && !compare.available" class="state-box notice">
            <strong>不显示环比</strong>
            <p>{{ compare.reason }}。为避免给出误导性数字，此处不展示同比/环比变化。</p>
          </section>

          <section class="kpi-grid">
            <article v-for="k in kpis" :key="k.label" class="card kpi">
              <div class="kpi-top">
                <span>{{ k.label }}</span
                ><span class="kpi-icon">{{ k.icon }}</span>
              </div>
              <div class="kpi-value" :title="brief(k).full">
                {{ brief(k).value }} <small>{{ brief(k).unit }}</small>
              </div>
              <div v-if="k.change" class="change" :class="k.dir">
                {{ k.dir === 'up' ? '↑' : k.dir === 'down' ? '↓' : '' }} {{ compare && compare.type
                }}{{ k.change }}
                <span v-if="k.compare_value" class="muted">（上期 {{ k.compare_value }}）</span>
              </div>
              <div v-else class="change muted">{{ k.note || '—' }}</div>
              <div class="kpi-exact" :title="`精确值 ${k.value} ${k.unit || ''}`">
                {{ k.value }} <small>{{ k.unit }}</small>
              </div>
              <div v-if="k.threshold_hint" class="kpi-hint">{{ k.threshold_hint }}</div>
            </article>
          </section>

          <section class="chart-grid">
            <article class="card chart-card">
              <div class="card-head">
                <h2>收入趋势</h2>
                <span>最近 {{ tail.months.length }} 个月 · {{ scaleUnit.name }}</span>
              </div>
              <div ref="revenueEl" class="chart" />
              <p class="chart-note">{{ revenueNote }}</p>
            </article>
            <article class="card chart-card">
              <div class="card-head">
                <h2>费用构成</h2>
                <span>按部门 Top 5 · {{ scaleUnit.name }}</span>
              </div>
              <div ref="expenseEl" class="chart" />
              <p class="chart-note">{{ notes.expenses }}</p>
            </article>
            <article class="card chart-card">
              <div class="card-head">
                <h2>现金流对比</h2>
                <span>单位：{{ scaleUnit.name }}</span>
              </div>
              <div v-if="data.cash.available !== false" ref="cashEl" class="chart" />
              <div v-else class="chart chart-unavailable">
                <strong>现金流口径待核验</strong>
                <p>当前字段均为零，不能据此认定实际现金流为零。</p>
              </div>
              <p class="chart-note">{{ notes.cash }}</p>
            </article>
          </section>

          <section class="card table-card">
            <div class="card-head">
              <h2>
                预警摘要<span v-if="alertTotal" class="muted"> · 共 {{ alertTotal }} 条（本期范围）</span>
              </h2>
              <ExportMenu kind="alerts" :period-from="alertRange.from" :period-to="alertRange.to" />
              <button class="btn" @click="router.push('/analysis/alerts')">查看全部</button>
            </div>
            <div v-if="!alerts.length" class="state-box notice">
              <p>所选期间未触发已配置的预警规则，可继续查看明细或发起 AI 分析。</p>
            </div>
            <div v-else class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>预警类型</th>
                    <th>描述</th>
                    <th>级别</th>
                    <th>期间</th>
                    <th>发生时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="a in alerts" :key="a.id">
                    <td>{{ a.type }}</td>
                    <td>{{ a.desc }}</td>
                    <td>
                      <span class="level" :class="a.level"
                        >● {{ a.level === 'high' ? '高' : a.level === 'medium' ? '中' : '低' }}</span
                      >
                    </td>
                    <td>{{ a.period || '—' }}</td>
                    <td>{{ humanTime(a.time) }}</td>
                    <td>
                      <div class="alert-actions">
                        <router-link class="link" :to="`/alerts/${a.id}`">查看明细 →</router-link
                        ><router-link class="link" :to="{ path: '/agent', query: { alert: a.id } }"
                          >✦ 分析此异常</router-link
                        >
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <p class="source-note">{{ data.source_note }}</p>
        </template>
      </template>

      <section class="analysis-entry card">
        <div>
          <span class="eyebrow">✦ AI 分析助手</span>
          <h2>数字背后，还有哪些值得追问？</h2>
          <p>查询财务指标、对比费用明细，并查阅相关制度依据。</p>
        </div>
        <router-link class="btn primary" to="/agent">进入 Agent 工作台 →</router-link>
      </section>
      <div class="footer">智能财务分析平台 · 数据仅供经营分析参考</div>
    </main>
  </div>
</template>
