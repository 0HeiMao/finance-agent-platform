<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const route = useRoute()
const sidebarOpen = ref(false)
const result = ref(null)
const error = ref('')
const loading = ref(true)
const saving = ref(false)

const STATUS_OPTIONS = [
  { value: 'confirmed', label: '确认异常' },
  { value: 'resolved', label: '已处理' },
  { value: 'false_positive', label: '误报' },
  { value: 'pending', label: '退回待处理' }
]
const alert = computed(() => result.value?.alert || null)

function money(value) {
  const n = Number(value || 0)
  return Number.isFinite(n)
    ? n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : String(value ?? '—')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await apiFetch(`/analysis/alerts/${route.params.id}/transactions`)
    result.value = await r.json()
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function review(next) {
  if (!alert.value) return
  let note = alert.value.note || ''
  try {
    // 处理动作都要留下「为什么」：误报必填，其它状态允许顺手补一句
    const { value } = await ElMessageBox.prompt(
      next === 'false_positive'
        ? '请说明为什么判定为误报（会写入备注并留痕）'
        : '可补充处理说明（留空则沿用原备注）',
      `标记为「${STATUS_OPTIONS.find((o) => o.value === next).label}」`,
      {
        confirmButtonText: '提交',
        cancelButtonText: '取消',
        inputValue: note,
        inputPlaceholder: '处理说明、后续动作、责任人…',
        inputValidator: (text) =>
          next !== 'false_positive' || (text && text.trim().length >= 4) || '请至少写 4 个字'
      }
    )
    note = (value || '').trim()
  } catch {
    return
  }
  saving.value = true
  try {
    const response = await apiFetch(`/analysis/alerts/${alert.value.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: next, note })
    })
    result.value.alert = await response.json()
    ElMessage.success(`已标记为「${result.value.alert.status_label}」`)
  } catch (e) {
    ElMessage.error(e && e.message ? e.message : '处理失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>预警交易明细</h1>
            <p>该预警对应期间的全部明细账，最多展示 200 行。</p>
          </div>
        </div>
        <div class="actions">
          <router-link class="btn" to="/analysis/alerts">← 返回预警监控</router-link>
        </div>
      </header>

      <section class="page-card">
        <p v-if="loading" class="muted">正在加载…</p>

        <section v-else-if="error" class="state-box error-state">
          <strong>加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="$router.go(0)">重试</button>
        </section>

        <section v-else-if="result && !result.alert" class="state-box empty-state">
          <strong>{{ result.message || '预警不存在' }}</strong>
          <p>该预警可能已被重新计算覆盖，请回到预警监控查看当前有效预警。</p>
          <router-link class="btn" to="/analysis/alerts">查看预警列表</router-link>
        </section>

        <template v-else-if="result">
          <div class="result-box">
            <strong>{{ result.alert.type }}</strong
            >：{{ result.alert.description }}
            <span class="muted">（级别 {{ result.alert.level }}）</span>
            <span class="status-chip" :class="result.alert.status">{{ result.alert.status_label }}</span>
            <small v-if="result.alert.note" class="note-text">备注：{{ result.alert.note }}</small>
            <small v-if="result.alert.reviewed_by" class="muted">
              —— {{ result.alert.reviewed_by }} 于
              {{ result.alert.reviewed_at.slice(0, 16).replace('T', ' ') }}
            </small>
          </div>
          <div class="ops">
            <button
              v-for="option in STATUS_OPTIONS"
              :key="option.value"
              class="btn"
              :disabled="saving || result.alert.status === option.value"
              @click="review(option.value)"
            >
              {{ option.label }}
            </button>
            <span class="muted">处理状态随重算保留，不会因为重新导入而丢失。</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th>科目</th>
                  <th>部门</th>
                  <th>收入</th>
                  <th>成本</th>
                  <th>费用</th>
                  <th>摘要</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in result.data" :key="row.id">
                  <td>{{ row.date }}</td>
                  <td>{{ row.account }}</td>
                  <td>{{ row.department }}</td>
                  <td>{{ money(row.revenue) }}</td>
                  <td>{{ money(row.cost) }}</td>
                  <td>{{ money(row.expense) }}</td>
                  <td>{{ row.description }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!result.data || !result.data.length" class="muted">
            该期间没有明细行（指标仍按期间汇总计算）。
          </p>
        </template>
      </section>
    </main>
  </div>
</template>

<style scoped>
.status-chip {
  display: inline-block;
  margin-left: 8px;
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
.note-text {
  display: block;
  margin-top: 4px;
  color: #8a6d3b;
}
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 12px 0;
}
</style>
