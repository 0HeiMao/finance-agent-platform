<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch } from '../api/client'
import { activeScope, withScope } from '../api/scope'
import AppSidebar from '../components/AppSidebar.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import ExportMenu from '../components/ExportMenu.vue'

const props = defineProps({ type: { type: String, default: 'profit' } })
const router = useRouter()
const sidebarOpen = ref(false)
const TITLES = { revenue: '收入分析', cost: '成本费用', profit: '利润分析', summary: '财务汇总' }

const period = ref('')
const data = ref(null)
const loading = ref(false)
const error = ref('')

const title = computed(() => TITLES[props.type] || '财务分析')
const cards = computed(() => {
  const d = data.value
  if (!d || !d.has_data) return []
  return [
    { label: '收入', value: money(d.revenue_amount), unit: '元' },
    { label: '成本', value: money(d.cost_amount), unit: '元' },
    { label: '净利润', value: money(d.net_profit_amount), unit: '元' },
    { label: '净利率', value: `${(Number(d.net_profit_ratio || 0) * 100).toFixed(2)}`, unit: '%' }
  ]
})

function money(value) {
  return Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

async function load() {
  if (!period.value) return
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch(withScope(`/analysis/summary?period=${period.value}`))
    data.value = await response.json()
  } catch (err) {
    data.value = null
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

// 默认期间取库里最新一期：演示数据是 2026-04~2026-10，用自然当前月会直接落到空状态
async function bootstrap() {
  try {
    const response = await apiFetch('/analysis/alerts/all?limit=1')
    const payload = await response.json()
    period.value = (payload.periods && payload.periods[0]) || new Date().toISOString().slice(0, 7)
  } catch {
    period.value = new Date().toISOString().slice(0, 7)
  }
  await load()
}

onMounted(bootstrap)
// 口径切换后重新取数（同理：指标数字必须和标签口径一致）。
watch(activeScope, load)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>{{ title }}</h1>
            <p>按期间查看程序计算的汇总指标。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
          <input v-model="period" type="month" class="select" @change="load" />
          <ExportMenu kind="transactions" :period="period" />
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '↻ 刷新' }}</button>
          <button class="btn" @click="router.push('/dashboard')">返回看板</button>
        </div>
      </header>

      <DataScopeNotice :scope="data && data.scope" />

      <section class="page-card">
        <section v-if="error" class="state-box error-state">
          <strong>指标加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="load">重试</button>
        </section>

        <div v-else-if="loading && !data" class="muted">正在加载…</div>

        <section v-else-if="!data || !data.has_data" class="state-box empty-state">
          <strong>该期间暂无数据</strong>
          <p>{{ period }} 没有计算过指标，请先导入该期间的财务数据。</p>
          <button class="btn" @click="router.push('/financial/import')">去导入数据</button>
        </section>

        <div v-else class="analysis-grid">
          <div v-for="card in cards" :key="card.label">
            <span>{{ card.label }}</span>
            <strong>{{ card.value }} {{ card.unit }}</strong>
          </div>
        </div>
        <p v-if="data && data.has_data" class="chart-note">
          口径：收入/成本/费用均为程序确定性汇总，净利率 = 净利润 ÷ 收入，保留四位小数。
        </p>
      </section>
    </main>
  </div>
</template>
