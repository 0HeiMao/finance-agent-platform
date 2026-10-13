<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiFetch } from '../api/client'
import { activeScope, withScope } from '../api/scope'
import AppSidebar from '../components/AppSidebar.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import PagePager from '../components/PagePager.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import ExportMenu from '../components/ExportMenu.vue'

const PAGE_SIZE = 50

const router = useRouter()
const sidebarOpen = ref(false)
const items = ref([])
const periods = ref([])
const period = ref('')
const status = ref('')
const scope = ref(null)
const stats = ref(null)
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
const savingId = ref(0)

const levelText = { high: '高', medium: '中', low: '低' }
// 与后端 REVIEW_STATUSES 对齐；「全部」用空串表示不筛选
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '待处理' },
  { value: 'confirmed', label: '已确认' },
  { value: 'false_positive', label: '误报' },
  { value: 'resolved', label: '已处理' }
]
const empty = computed(() => loaded.value && !items.value.length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
    if (period.value) params.set('period', period.value)
    // 状态筛选走后端：筛选必须发生在分页之前，否则「只看误报」只能看到恰好落在本页的误报
    if (status.value) params.set('status', status.value)
    const response = await apiFetch(withScope(`/analysis/alerts/all?${params.toString()}`))
    const payload = await response.json()
    items.value = payload.data || []
    total.value = payload.total || 0
    periods.value = payload.periods || []
    scope.value = payload.scope || null
    // 默认停在「全部期间」：这个页面是来处理积压的，上来就只看一个月会把待处理数藏起来。
    // 注意别在这里顺手把 period 设成最新期间——那会让列表（无筛选）与统计（被筛选）对不上。
    await loadStats()
  } catch (err) {
    items.value = []
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

// 统计只跟期间走，不跟状态筛选走：一筛「待处理」就再也看不到自己在处理什么了
async function loadStats() {
  try {
    const query = period.value ? `?period=${period.value}` : ''
    const response = await apiFetch(withScope(`/analysis/alerts/stats${query}`))
    stats.value = (await response.json()).counts || null
  } catch {
    stats.value = null
  }
}

function reload() {
  offset.value = 0
  load()
}

function turn(next) {
  offset.value = next
  load()
}

async function review(item, next, note = '') {
  savingId.value = item.id
  try {
    const response = await apiFetch(`/analysis/alerts/${item.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: next, note })
    })
    const updated = await response.json()
    Object.assign(item, updated)
    ElMessage.success(`已标记为「${updated.status_label}」`)
    // 状态筛选在服务端：改完状态这行可能已经不属于当前筛选，重新取当前页才不会自相矛盾
    await load()
  } catch (err) {
    ElMessage.error(err && err.message ? err.message : '处理失败')
  } finally {
    savingId.value = 0
  }
}

async function markFalsePositive(item) {
  try {
    // 误报必须写原因：后端也会挡，但在这里先问一句，用户不用先撞一次 422
    const { value } = await ElMessageBox.prompt(
      '请说明为什么判定为误报（会写入备注并留痕）',
      `标记「${item.type}」为误报`,
      {
        confirmButtonText: '提交',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：该凭证为年末冲销，已在次月冲回',
        inputValidator: (text) => (text && text.trim().length >= 4) || '请至少写 4 个字'
      }
    )
    await review(item, 'false_positive', value.trim())
  } catch {
    /* 用户取消：什么都不做 */
  }
}

async function addNote(item) {
  try {
    const { value } = await ElMessageBox.prompt(
      '备注会保留在预警上，重算期间不会丢失',
      `「${item.type}」备注`,
      {
        confirmButtonText: '保存',
        cancelButtonText: '取消',
        inputValue: item.note || '',
        inputPlaceholder: '处理说明、后续动作、责任人…'
      }
    )
    await review(item, item.status === 'pending' ? 'confirmed' : item.status, value.trim())
  } catch {
    /* 取消 */
  }
}

onMounted(load)
watch(activeScope, load)
// 换期间要连列表一起重取（load 内部会再刷新统计）
watch(period, reload)
// 状态筛选改由后端做（筛选要在分页之前，否则只看得到当前页里的误报）；换筛选回到第 1 页
watch(status, reload)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>预警监控</h1>
            <p>按期间查看系统计算的异常预警；处理状态（确认 / 误报 / 已处理）会随重算保留。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
          <select v-model="period" class="select">
            <option value="">全部期间</option>
            <option v-for="p in periods" :key="p" :value="p">{{ p }}</option>
          </select>
          <select v-model="status" class="select">
            <option v-for="option in STATUS_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <ExportMenu kind="alerts" :period="period" />
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '↻ 刷新' }}</button>
          <button class="btn" @click="router.push('/dashboard')">返回看板</button>
        </div>
      </header>

      <DataScopeNotice :scope="scope" />

      <section v-if="stats" class="page-card stat-row">
        <div class="stat">
          <span>待处理</span>
          <strong class="pending">{{ stats.pending }}</strong>
        </div>
        <div class="stat">
          <span>已确认</span><strong>{{ stats.confirmed }}</strong>
        </div>
        <div class="stat">
          <span>误报</span><strong>{{ stats.false_positive }}</strong>
        </div>
        <div class="stat">
          <span>已处理</span><strong>{{ stats.resolved }}</strong>
        </div>
        <div class="stat">
          <span>合计</span><strong>{{ stats.total }}</strong>
        </div>
        <p class="muted stat-hint">
          处理状态只记在预警本身上：重算某期间（导入、撤销批次）时会按「口径 + 类型 + 指标」继承，
          已经点过的结论不会因为重算而消失。
        </p>
      </section>

      <section class="page-card">
        <section v-if="error" class="state-box error-state">
          <strong>预警加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="load">重试</button>
        </section>

        <div v-else-if="loading && !items.length" class="muted">正在加载…</div>

        <section v-else-if="empty && !status" class="state-box empty-state">
          <strong>没有预警记录</strong>
          <p>所选期间未触发预警。源数据没有成本科目，「成本率异常」不会触发，只有「利润异常」可能命中。</p>
        </section>

        <section v-else-if="empty && status" class="state-box empty-state">
          <strong>该状态下没有预警</strong>
          <p>
            当前筛选为「{{
              STATUS_OPTIONS.find((o) => o.value === status).label
            }}」，换个状态或选「全部状态」。
          </p>
        </section>

        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>期间</th>
                <th>预警类型</th>
                <th>描述</th>
                <th>级别</th>
                <th>处理状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in items" :key="a.id">
                <td>{{ a.period }}</td>
                <td>
                  {{ a.type }}
                  <small v-if="a.department" class="muted">（部门 {{ a.department }}）</small>
                </td>
                <td>
                  {{ a.description }}
                  <small v-if="a.note" class="note-text">备注：{{ a.note }}</small>
                  <small v-if="a.reviewed_by" class="muted">
                    —— {{ a.reviewed_by }} 于 {{ a.reviewed_at.slice(0, 16).replace('T', ' ') }}
                  </small>
                </td>
                <td>
                  <span class="level" :class="a.level">● {{ levelText[a.level] || a.level }}</span>
                </td>
                <td>
                  <span class="status-chip" :class="a.status">{{ a.status_label }}</span>
                </td>
                <td class="ops">
                  <button
                    v-if="a.status !== 'confirmed'"
                    class="link-btn"
                    :disabled="savingId === a.id"
                    @click="review(a, 'confirmed')"
                  >
                    确认异常
                  </button>
                  <button
                    v-if="a.status !== 'false_positive'"
                    class="link-btn"
                    :disabled="savingId === a.id"
                    @click="markFalsePositive(a)"
                  >
                    误报
                  </button>
                  <button
                    v-if="a.status !== 'resolved'"
                    class="link-btn"
                    :disabled="savingId === a.id"
                    @click="review(a, 'resolved')"
                  >
                    已处理
                  </button>
                  <button class="link-btn" :disabled="savingId === a.id" @click="addNote(a)">备注</button>
                  <button
                    v-if="a.status !== 'pending'"
                    class="link-btn"
                    :disabled="savingId === a.id"
                    @click="review(a, 'pending')"
                  >
                    退回
                  </button>
                  <router-link class="link" :to="`/alerts/${a.id}`">明细 →</router-link>
                  <router-link class="link" :to="{ path: '/agent', query: { alert: a.id } }"
                    >分析此异常</router-link
                  >
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PagePager
          :total="total"
          :limit="PAGE_SIZE"
          :offset="offset"
          :loading="loading"
          unit="条预警"
          @change="turn"
        />
      </section>
    </main>
  </div>
</template>

<style scoped>
.stat-row {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 28px;
  align-items: flex-end;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 64px;
}
.stat span {
  font-size: 12px;
  color: #718096;
}
.stat strong {
  font-size: 20px;
  color: #1f2937;
}
.stat strong.pending {
  color: #d46b08;
}
.stat-hint {
  flex-basis: 100%;
  margin: 0;
  font-size: 12px;
}
.note-text {
  display: block;
  margin-top: 3px;
  color: #8a6d3b;
}
.status-chip {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #eef3f9;
}
.status-chip.pending {
  background: #fff7e6;
  color: #d46b08;
}
.status-chip.confirmed {
  background: #fff1f0;
  color: #cf1322;
}
.status-chip.false_positive {
  background: #f6ffed;
  color: #237804;
}
.status-chip.resolved {
  background: #e6f4ff;
  color: #0958d9;
}
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.link-btn {
  border: none;
  background: none;
  color: #1a5c9c;
  cursor: pointer;
  padding: 0;
  font-size: 13px;
}
.link-btn:disabled {
  color: #a0aec0;
  cursor: not-allowed;
}
</style>
