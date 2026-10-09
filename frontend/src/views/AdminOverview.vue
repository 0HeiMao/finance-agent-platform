<script setup>
import { computed, onMounted, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import ExportMenu from '../components/ExportMenu.vue'

const sidebarOpen = ref(false)
const data = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/overview')
    data.value = await response.json()
  } catch (e) {
    error.value = `读取管理概览失败：${e.message}`
    data.value = null
  } finally {
    loading.value = false
  }
}

const COUNT_LABELS = [
  ['users', '用户', '人'],
  ['admins', '其中管理员', '人'],
  ['transactions', '交易明细', '行'],
  ['metrics', '期间指标', '期'],
  ['alerts', '预警', '条'],
  ['knowledge_chunks', '知识库片段', '条'],
  ['knowledge_sources', '知识库来源', '份'],
  ['batches', '导入批次', '个']
]

// 只看最慢的几条路径：全局 P95 说明整体感受，路径 P95 才指出该去优化谁。
const SLOW_PATH_LIMIT = 5
const slowPaths = computed(() => {
  const paths = data.value?.observability?.paths || {}
  return Object.entries(paths)
    .map(([path, stat]) => ({ path, ...stat }))
    .sort((a, b) => b.p95_ms - a.p95_ms)
    .slice(0, SLOW_PATH_LIMIT)
})

// 超过阈值的路径：这些是「已经该有人去看一眼」的，按超阈值次数排序（后端已排好）。
// 与「最慢的接口」的区别是：重接口的阈值被特意放宽，P95 高但没有一次超过阈值就不算告警。
const slowAlerts = computed(() => data.value?.observability?.slow_paths || [])
const slowDefaultMs = computed(() => data.value?.observability?.slow_default_ms ?? 1000)
function isOverThreshold(row) {
  return row.p95_ms >= (data.value?.observability?.slow_overrides?.[row.path] ?? slowDefaultMs.value)
}

// 季/年落库：正常路径由重算指标自动刷新，这里给「老库回填」和「怀疑数字不对时手工重算」兜底。
const rebuilding = ref(false)
const rebuildNotice = ref('')
const coverage = computed(
  () =>
    data.value?.rollups || {
      rows: 0,
      company_rows: 0,
      department_rows: 0,
      quarters: [],
      years: [],
      incomplete: []
    }
)

async function rebuildRollups() {
  rebuilding.value = true
  rebuildNotice.value = ''
  try {
    const response = await apiFetch('/admin/rollups/rebuild', { method: 'POST', body: JSON.stringify({}) })
    const payload = await response.json()
    rebuildNotice.value = `已重算 ${payload.rows} 行季/年合计`
    await load()
  } catch (e) {
    rebuildNotice.value = `重算失败：${e.message}`
  } finally {
    rebuilding.value = false
  }
}

// Agent 用量（⑮）：跨用户看「谁在烧 token」。单独一个请求、单独一套错误状态——
// 用量统计失败不该把整页概览变成错误页（它与财务数据是两条链路）。
const AGENT_USAGE_DAYS = 7
const agentUsage = ref(null)
const agentUsageError = ref('')
const byUser = computed(() => agentUsage.value?.data?.by_user || [])

async function loadAgentUsage() {
  agentUsageError.value = ''
  try {
    const response = await apiFetch(`/admin/agent-usage?days=${AGENT_USAGE_DAYS}`)
    agentUsage.value = await response.json()
  } catch (e) {
    agentUsage.value = null
    agentUsageError.value = `读取 Agent 用量失败：${e.message}`
  }
}

// 「重新读取」要连着用量一起刷：用量卡有自己的错误状态，只刷一半会留下过期数字。
function reload() {
  load()
  loadAgentUsage()
}

onMounted(reload)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>管理概览</h1>
            <p>数据规模、完整性体检与接口运行观测；只有不受部门限制的账号可以进入。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="reload">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <p v-if="loading && !data" class="muted">正在读取管理概览…</p>

      <section v-else-if="error && !data" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="reload">重试</button>
      </section>

      <template v-else-if="data">
        <section v-if="error" class="state-box error-state">
          <strong>操作未完成</strong>
          <p>{{ error }}</p>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>数据规模</h2>
            <span>截止本次读取，全部来自数据库实时计数</span>
          </div>
          <div class="settings-grid">
            <div v-for="[key, label, unit] in COUNT_LABELS" :key="key">
              <span>{{ label }}</span>
              <strong>{{ data.counts[key] }} {{ unit }}</strong>
            </div>
            <div>
              <span>最新期间</span>
              <strong>{{ data.periods.latest || '（无）' }}</strong>
              <small>{{ data.periods.all.length }} 个可用期间</small>
            </div>
            <div>
              <span>当前账号</span>
              <strong>{{ data.me.username }}</strong>
              <small>id {{ data.me.id }} · 不受部门限制</small>
            </div>
          </div>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>数据完整性体检</h2>
            <span>指标与明细对不上、口径字段缺失这类问题一律先亮出来</span>
          </div>
          <ul class="dir-list">
            <li v-for="check in data.quality" :key="check.name">
              <div class="dir-head">
                <strong>{{ check.name }}</strong>
                <span class="level" :class="check.level">{{ check.value }}</span>
              </div>
              <small class="quality-hint" :class="{ bad: check.level !== 'ok' }">{{ check.hint }}</small>
            </li>
          </ul>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>季度/年度落库</h2>
            <span>季/年合计是算好存下来的，不是每次请求现算</span>
          </div>
          <div class="settings-grid">
            <div>
              <span>落库合计行</span>
              <strong>{{ coverage.rows }} 行</strong>
              <small
                >全公司 {{ coverage.company_rows }} 行 · 部门口径 {{ coverage.department_rows }} 行</small
              >
            </div>
            <div>
              <span>已落季度 / 年度</span>
              <strong>{{ coverage.quarters.length }} / {{ coverage.years.length }} 期</strong>
              <small>{{ coverage.quarters.join('、') || '暂无' }}</small>
            </div>
          </div>
          <h3 class="sub-head">缺月的期间（合计不是整期）</h3>
          <p v-if="!coverage.incomplete.length" class="muted">所有季/年都凑齐了完整月份。</p>
          <ul v-else class="dir-list">
            <li v-for="row in coverage.incomplete" :key="`${row.grain}-${row.period}-${row.department}`">
              <div class="dir-head">
                <strong
                  >{{ row.period_label
                  }}<span v-if="row.department"> · {{ row.department_label }}</span></strong
                >
                <span class="level high">缺 {{ row.missing_months.length }} 个月</span>
              </div>
              <small class="quality-hint bad"
                >缺 {{ row.missing_months.join('、') }}；这些月份没有导入数据，合计只覆盖已有的月份</small
              >
            </li>
          </ul>
          <div class="card-actions">
            <button class="btn" :disabled="rebuilding" @click="rebuildRollups">
              {{ rebuilding ? '重算中…' : '重算季/年落库' }}
            </button>
            <!-- 屏幕上就是季/年合计，导出也必须能按季/年导（原来只导得出月行）；「是否完整」那一列
                 会把 `complete=False` 的期间标出来，「年度合计看起来像全年」就不会骗到人（⑲） -->
            <ExportMenu kind="metrics" grain="quarter" label="导出季度" />
            <ExportMenu kind="metrics" grain="year" label="导出年度" />
            <small v-if="rebuildNotice" class="quality-hint">{{ rebuildNotice }}</small>
            <small v-else class="quality-hint"
              >重算指标、导入、撤销批次都会自动刷新对应的季与年；这里用于老库回填。</small
            >
          </div>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>最近导入批次</h2>
            <router-link class="link" to="/admin/batches">去批次管理 →</router-link>
          </div>
          <p v-if="!data.recent_batches.length" class="muted">还没有任何导入批次。</p>
          <ul v-else class="dir-list">
            <li v-for="batch in data.recent_batches" :key="batch.batch">
              <div class="dir-head">
                <strong>{{ batch.short }}…</strong>
                <span class="level low">{{ batch.rows }} 行</span>
              </div>
              <small class="quality-hint"
                >{{ batch.first_date }} ~ {{ batch.last_date }} · {{ batch.periods.length }} 个期间 · 导入于
                {{ batch.imported_at }}</small
              >
            </li>
          </ul>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>接口运行观测</h2>
            <span>进程内计数，重启即清零</span>
          </div>
          <div class="settings-grid">
            <div>
              <span>累计请求</span>
              <strong>{{ data.observability.total }} 次</strong>
              <small>按状态码与路径分桶 {{ Object.keys(data.observability.requests).length }} 个</small>
            </div>
            <div>
              <span>P95 响应耗时</span>
              <strong>{{ data.observability.p95_duration_ms }} ms</strong>
              <small>仅统计最近 1000 次采样</small>
            </div>
          </div>
          <h3 class="sub-head">最慢的接口（按路径 P95）</h3>
          <p v-if="!slowPaths.length" class="muted">本次进程还没有请求记录。</p>
          <ul v-else class="dir-list">
            <li v-for="row in slowPaths" :key="row.path">
              <div class="dir-head">
                <strong class="path-text">{{ row.path }}</strong>
                <span class="level" :class="isOverThreshold(row) ? 'high' : 'low'">{{ row.p95_ms }} ms</span>
              </div>
              <small class="quality-hint">采样 {{ row.count }} 次 · 最慢 {{ row.max_ms }} ms</small>
            </li>
          </ul>
          <h3 class="sub-head">慢接口告警（单次超过阈值）</h3>
          <p v-if="!slowAlerts.length" class="muted">
            本次进程没有请求超过阈值（默认 {{ slowDefaultMs }} ms，重接口可在 .env 里用
            <code>SLOW_PATH_OVERRIDES</code> 单独放宽）。
          </p>
          <ul v-else class="dir-list">
            <li v-for="row in slowAlerts" :key="row.path">
              <div class="dir-head">
                <strong class="path-text">{{ row.path }}</strong>
                <span class="level high">超阈值 {{ row.exceeded }} 次</span>
              </div>
              <small class="quality-hint">
                阈值 {{ row.threshold_ms }} ms · 最慢 {{ row.worst_ms }} ms · 最近 {{ row.last_ms }} ms（{{
                  row.last_at
                }}）
              </small>
            </li>
          </ul>
          <p class="quality-hint">
            每路径只保留最近 {{ data.observability.samples_per_path }} 次采样；进程重启即清零。
          </p>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>Agent 用量</h2>
            <span>近 {{ AGENT_USAGE_DAYS }} 天，按回答统计</span>
          </div>
          <p v-if="agentUsageError" class="muted">{{ agentUsageError }}</p>
          <template v-else-if="agentUsage">
            <div class="settings-grid">
              <div>
                <span>回答</span>
                <strong>{{ agentUsage.data.answers }} 条</strong>
                <small
                  >提问 {{ agentUsage.data.questions }} 次 · 会话 {{ agentUsage.data.sessions }} 个</small
                >
              </div>
              <div>
                <span>总 tokens</span>
                <strong>{{ agentUsage.data.total_tokens }}</strong>
                <small
                  >账单价 {{ agentUsage.data.api_answers }} 条 · 估算
                  {{ agentUsage.data.estimated_answers }} 条</small
                >
              </div>
            </div>
            <h3 class="sub-head">按用户（按 tokens 排序）</h3>
            <p v-if="!byUser.length" class="muted">这段时间还没有人用过智能助手。</p>
            <ul v-else class="dir-list">
              <li v-for="row in byUser" :key="row.username">
                <div class="dir-head">
                  <strong>{{ row.username }}</strong>
                  <span class="level low">{{ row.total_tokens }} tokens</span>
                </div>
                <small class="quality-hint">{{ row.department }} · {{ row.answers }} 条回答</small>
              </li>
            </ul>
            <h3 class="sub-head">逐日</h3>
            <ul class="dir-list">
              <li v-for="row in agentUsage.series" :key="row.date">
                <div class="dir-head">
                  <strong class="path-text">{{ row.date }}</strong>
                  <span class="level low">{{ row.answers }} 条</span>
                </div>
                <small class="quality-hint">{{ row.total_tokens }} tokens</small>
              </li>
            </ul>
            <p class="quality-hint">
              估算的 token 不是账单（模型没报用量时按字数折算），窗口是含今天的
              {{ agentUsage.data.days }} 个自然日（UTC），没问的日子也会占一行。
            </p>
          </template>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>管理入口</h2></div>
          <ul class="dir-list">
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/users">用户管理</router-link></strong>
                <span class="level low">{{ data.counts.users }} 人</span>
              </div>
              <small class="quality-hint">改部门＝改数据可见范围；部门为空即管理员</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/departments">部门权限</router-link></strong>
              </div>
              <small class="quality-hint">部门决定财务数据与知识库的可见范围；部门为空即不受限</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/alert-rules">预警规则</router-link></strong>
              </div>
              <small class="quality-hint">阈值与开关存在库里，改完在下一次重算（导入/撤销批次）时生效</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/audit">审计日志</router-link></strong>
              </div>
              <small class="quality-hint"
                >导入、撤销批次、删账号、删知识库、改规则、处理预警、导出都会留痕</small
              >
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/batches">导入批次</router-link></strong>
                <span class="level low">{{ data.counts.batches }} 个</span>
              </div>
              <small class="quality-hint">撤销批次会同步重算受影响期间，不会留下对不上的历史指标</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/knowledge">知识库管理</router-link></strong>
                <span class="level low">{{ data.counts.knowledge_chunks }} 条</span>
              </div>
              <small class="quality-hint">按来源整份查看与删除，上传接口本身按内容哈希去重</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/settings">System 设置</router-link></strong>
              </div>
              <small class="quality-hint">配置体检、目录可写性、模型与检索口径</small>
            </li>
          </ul>
        </section>
      </template>
    </main>
  </div>
</template>
