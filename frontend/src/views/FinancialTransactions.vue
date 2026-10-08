<script setup>
import { computed, onMounted, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'

const PAGE_SIZE = 50
// 可更正的字段：中文标签 + 输入类型。金额一律按字符串发给后端，由后端用导入那一套规则解析，
// 前端不做金额运算，避免"前端认为合法、后端拒绝"的双份实现慢慢分家。
const FIELDS = [
  { key: 'transaction_date', label: '交易日期', type: 'date' },
  { key: 'account_code', label: '科目编码', type: 'text' },
  { key: 'account_name', label: '科目名称', type: 'text' },
  { key: 'department', label: '部门', type: 'text', hint: '留空 = 记在全公司口径下，只有不受限账号能改' },
  { key: 'description', label: '摘要', type: 'text' },
  { key: 'revenue_amount', label: '收入金额', type: 'amount' },
  { key: 'cost_amount', label: '成本金额', type: 'amount' },
  { key: 'expense_amount', label: '费用金额', type: 'amount' },
  { key: 'cash_in_amount', label: '现金流入', type: 'amount' },
  { key: 'cash_out_amount', label: '现金流出', type: 'amount' }
]

const sidebarOpen = ref(false)
const rows = ref([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busy = ref(0)
const periods = ref([])
const scope = ref({})
const periodFilter = ref('')
const departmentFilter = ref('')
const queryFilter = ref('')
const batchFilter = ref('')
const departmentOptions = ref([])
// 正在编辑的行：editId + draft（按字段存的字符串）。null = 没有在编辑。
const editId = ref(null)
const draft = ref({})
const confirmReverse = ref(0)
const reverseReason = ref('')

const scopeLabel = computed(() => scope.value?.label || '全公司（全部部门）')

function amount(value) {
  return Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function statusOf(row) {
  if (row.is_reversal) return '冲销行'
  if (row.reversed) return '已冲销'
  if (row.corrections) return `已更正 ${row.corrections} 次`
  return '正常'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
    if (periodFilter.value) params.set('period', periodFilter.value)
    if (departmentFilter.value) params.set('department', departmentFilter.value)
    if (queryFilter.value.trim()) params.set('query', queryFilter.value.trim())
    if (batchFilter.value.trim()) params.set('batch', batchFilter.value.trim())
    const response = await apiFetch(`/financial/transactions?${params.toString()}`)
    const payload = await response.json()
    rows.value = payload.data
    total.value = payload.total || 0
    periods.value = payload.periods || []
    scope.value = payload.scope || {}
  } catch (e) {
    error.value = `读取明细失败：${e.message}`
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function loadDepartments() {
  try {
    const response = await apiFetch('/analysis/departments')
    const payload = await response.json()
    departmentOptions.value = payload.departments || []
  } catch {
    // 口径下拉读不到不影响列表主体，静默降级成"全部部门"
    departmentOptions.value = []
  }
}

function turn(next) {
  offset.value = next
  load()
}

function applyFilters() {
  offset.value = 0
  load()
}

function startEdit(row) {
  editId.value = row.id
  const next = {}
  for (const field of FIELDS) {
    const value = row[field.key]
    if (field.type === 'date') next[field.key] = (row.transaction_date || '').slice(0, 10)
    else if (field.type === 'amount')
      next[field.key] = value === null || value === undefined ? '' : String(value)
    else next[field.key] = value ?? ''
  }
  draft.value = next
}

function cancelEdit() {
  editId.value = null
  draft.value = {}
}

// 只把真正改过的字段发出去：后端会拒绝"没有要更正的字段"和"改动的值与原值一致"，
// 但那是兜底；前端先算差集，用户点保存时才知道自己到底改了什么。
function changedFields() {
  const row = rows.value.find((item) => item.id === editId.value)
  const changes = {}
  for (const field of FIELDS) {
    const before =
      field.type === 'date'
        ? (row.transaction_date || '').slice(0, 10)
        : field.type === 'amount'
          ? String(row[field.key] ?? '')
          : String(row[field.key] ?? '')
    const after = field.type === 'date' ? draft.value[field.key] : draft.value[field.key].trim()
    if (after === '' && field.type === 'amount') continue
    if (after !== before) changes[field.key] = after
  }
  return changes
}

function describeRefresh(payload) {
  const parts = []
  if (payload.recalculated?.length) parts.push(`已重算 ${payload.recalculated.join('、')}`)
  if (payload.cleared?.length) parts.push(`${payload.cleared.join('、')} 已无明细，指标与落库一并清除`)
  return parts.length ? `；${parts.join('；')}` : ''
}

async function saveEdit() {
  const changes = changedFields()
  if (!Object.keys(changes).length) {
    error.value = '没有要更正的字段（改完再保存）'
    return
  }
  busy.value = editId.value
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/financial/transactions/${editId.value}`, {
      method: 'PATCH',
      body: JSON.stringify(changes)
    })
    const payload = await response.json()
    notice.value = `已更正第 ${editId.value} 行，旧值写在审计日志里${describeRefresh(payload)}`
    cancelEdit()
    await load()
  } catch (e) {
    error.value = `更正失败：${e.message}`
  } finally {
    busy.value = 0
  }
}

async function reverse(row) {
  busy.value = row.id
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/financial/transactions/${row.id}/reverse`, {
      method: 'POST',
      body: JSON.stringify(reverseReason.value ? { reason: reverseReason.value } : {})
    })
    const payload = await response.json()
    notice.value = `已冲销第 ${row.id} 行：新增冲销行 #${payload.reversal.id}（金额取负），原行保留并标记${describeRefresh(payload)}`
    confirmReverse.value = 0
    reverseReason.value = ''
    await load()
  } catch (e) {
    error.value = `冲销失败：${e.message}`
  } finally {
    busy.value = 0
  }
}

onMounted(() => {
  load()
  loadDepartments()
})
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>明细更正</h1>
            <p>
              当前口径：{{ scopeLabel }}。更正就地改字段并记一次审计（旧值可追）；冲销不改原行，
              而是新增一条金额取负的冲销行，两行合计为 0，指标仍然等于真实账。
            </p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <section class="filter-card">
        <label
          >期间
          <select v-model="periodFilter" @change="applyFilters">
            <option value="">全部期间</option>
            <option v-for="item in periods" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label
          >部门（口径）
          <select v-model="departmentFilter" @change="applyFilters">
            <option value="">按当前账号范围</option>
            <option v-for="item in departmentOptions" :key="item.name" :value="item.name">
              {{ item.label || item.name }}（{{ item.transactions }} 行）
            </option>
          </select>
        </label>
        <label
          >关键字
          <input v-model.trim="queryFilter" placeholder="科目或摘要" @keyup.enter="applyFilters" />
        </label>
        <label
          >批次
          <input
            v-model.trim="batchFilter"
            placeholder="批次号（可只填前几位）"
            @keyup.enter="applyFilters"
          />
        </label>
        <button class="btn primary" @click="applyFilters">筛选</button>
      </section>

      <p v-if="loading && !rows.length" class="muted">正在读取明细…</p>

      <section v-else-if="error && !rows.length" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else>
        <section v-if="error" class="state-box error-state">
          <strong>操作未完成</strong>
          <p>{{ error }}</p>
        </section>
        <section v-else-if="notice" class="state-box notice">{{ notice }}</section>

        <section v-if="!rows.length" class="state-box empty-state">
          <strong>这个筛选条件下没有明细</strong>
          <p>换个期间或清空关键字试试；也可能这批数据已经被撤销批次删掉了。</p>
        </section>

        <section v-else class="page-card">
          <div class="card-head">
            <h2>共 {{ total }} 行明细</h2>
            <span>按交易日期倒序；冲销行与原行都要能看到，才解释得清这笔钱去哪了</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>id</th>
                  <th>日期</th>
                  <th>科目</th>
                  <th>部门</th>
                  <th>摘要</th>
                  <th>收入</th>
                  <th>成本</th>
                  <th>费用</th>
                  <th>批次</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="row in rows" :key="row.id">
                  <tr :class="{ reversed: row.reversed, reversal: row.is_reversal }">
                    <td>{{ row.id }}</td>
                    <td>{{ (row.transaction_date || '').slice(0, 10) }}</td>
                    <td>
                      <div>{{ row.account_name }}</div>
                      <small class="muted">{{ row.account_code }}</small>
                    </td>
                    <td>{{ row.department || '全公司' }}</td>
                    <td :title="row.description">{{ row.description || '—' }}</td>
                    <td>{{ amount(row.revenue_amount) }}</td>
                    <td>{{ amount(row.cost_amount) }}</td>
                    <td>{{ amount(row.expense_amount) }}</td>
                    <td :title="row.import_batch">
                      <code>{{ (row.import_batch || '').slice(0, 8) }}…</code>
                    </td>
                    <td>{{ statusOf(row) }}</td>
                    <td>
                      <div class="row-actions">
                        <template v-if="row.reversed || row.is_reversal">
                          <span class="muted">—</span>
                        </template>
                        <template v-else-if="confirmReverse === row.id">
                          <input
                            v-model.trim="reverseReason"
                            class="reason"
                            placeholder="冲销原因（可留空）"
                            maxlength="500"
                          />
                          <button class="btn danger" :disabled="busy === row.id" @click="reverse(row)">
                            {{ busy === row.id ? '冲销中…' : '确认冲销' }}
                          </button>
                          <button class="btn" @click="confirmReverse = 0">取消</button>
                        </template>
                        <template v-else>
                          <button class="btn" :disabled="editId === row.id" @click="startEdit(row)">
                            更正
                          </button>
                          <button class="btn danger" @click="confirmReverse = row.id">冲销</button>
                        </template>
                      </div>
                    </td>
                  </tr>
                  <tr v-if="editId === row.id" class="edit-row">
                    <td colspan="11">
                      <div class="edit-grid">
                        <label v-for="field in FIELDS" :key="field.key">
                          <span>{{ field.label }}</span>
                          <input
                            v-model="draft[field.key]"
                            :type="field.type === 'date' ? 'date' : 'text'"
                            :placeholder="field.type === 'amount' ? '按导入规则解析，留空=不改' : ''"
                            :maxlength="field.key === 'description' ? 2000 : 200"
                          />
                        </label>
                      </div>
                      <div class="card-actions">
                        <button class="btn primary" :disabled="busy === row.id" @click="saveEdit">
                          {{ busy === row.id ? '保存中…' : '保存更正' }}
                        </button>
                        <button class="btn" @click="cancelEdit">取消</button>
                        <small>
                          金额按导入同一套规则解析（支持千分位）；改日期会把这一行挪到新的期间，
                          旧期间会跟着重算，腾空的期间连指标与落库一起清掉。
                        </small>
                      </div>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
          <PagePager
            :total="total"
            :limit="PAGE_SIZE"
            :offset="offset"
            :loading="loading"
            unit="行明细"
            @change="turn"
          />
          <p class="muted">
            这一页不做删除：更正与冲销都会留痕（审计日志里能查到旧值与操作人），
            要整批收回请到「导入批次」撤销批次。
          </p>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped>
tr.reversed td {
  color: #8a8f99;
  text-decoration: line-through;
}
tr.reversal td {
  color: #a8071a;
}
tr.edit-row td {
  background: #f7fafd;
}
.edit-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.edit-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--muted);
}
.reason {
  max-width: 180px;
}
</style>
