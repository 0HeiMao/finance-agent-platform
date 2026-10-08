<script setup>
import { onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import { saveBlob } from '../api/download'
import { activeScope, withScope } from '../api/scope'
import AppSidebar from '../components/AppSidebar.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'

const sidebarOpen = ref(false)
const periods = ref([])
const period = ref('')
const outline = ref(null)
const loading = ref(false)
const downloading = ref(false)
const error = ref('')
const saved = ref('')

// 大纲里 kind 决定用哪种排版呈现：cover/chart 的条目是给人看的，table/bullets 用列表
const KIND_LABEL = { cover: '封面', table: '指标', bullets: '要点', chart: '图表' }

async function loadPeriods() {
  try {
    const r = await apiFetch('/analysis/alerts/all?limit=1')
    const payload = await r.json()
    periods.value = payload.periods || []
    if (periods.value.length && !period.value) period.value = periods.value[0]
  } catch (e) {
    error.value = `加载可用期间失败：${e.message}`
  }
}

async function loadOutline() {
  if (!period.value) return
  loading.value = true
  error.value = ''
  saved.value = ''
  try {
    // 口径跟在会话里选的一致：切到部门 097 后生成的报告必须就是 097 口径（后端会写入封面副标题）
    const r = await apiFetch(withScope(`/reports/pptx/outline?period=${encodeURIComponent(period.value)}`))
    outline.value = await r.json()
  } catch (e) {
    error.value = `生成大纲失败：${e.message}`
    outline.value = null
  } finally {
    loading.value = false
  }
}

async function download() {
  if (!period.value) return
  downloading.value = true
  error.value = ''
  try {
    // 走后端下载而不是在前端拼 pptx：二进制由 python-pptx 生成，前端只负责保存
    const r = await apiFetch(withScope(`/reports/pptx?period=${encodeURIComponent(period.value)}`))
    const blob = await r.blob()
    const filename = outline.value?.filename || `财务分析报告-${period.value}.pptx`
    saveBlob(blob, filename)
    saved.value = `已保存 ${filename}（${Math.round(blob.size / 1024)} KB）`
  } catch (e) {
    error.value = `下载失败：${e.message}`
  } finally {
    downloading.value = false
  }
}

onMounted(async () => {
  await loadPeriods()
  await loadOutline()
})
// 切口径后大纲作废重取（否则会拿着全公司的封面给部门报告）。
watch(activeScope, loadOutline)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>PPT 报告</h1>
            <p>按期间生成 .pptx 演示文稿（封面、核心指标、风险预警、费用构成图表、口径与限制）。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
          <select v-model="period" class="select" :disabled="!periods.length" @change="loadOutline">
            <option v-if="!periods.length" value="">暂无可用期间</option>
            <option v-for="p in periods" :key="p" :value="p">{{ p }}</option>
          </select>
          <button class="btn" :disabled="loading || !period" @click="loadOutline">
            {{ loading ? '生成中…' : '↻ 刷新大纲' }}
          </button>
        </div>
      </header>

      <p v-if="loading" class="muted">正在准备 {{ period }} 的大纲…</p>

      <section v-else-if="error" class="state-box error-state">
        <strong>操作未完成</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="loadOutline">重试</button>
      </section>

      <section v-else-if="!periods.length" class="state-box empty-state">
        <strong>还没有可导出的期间</strong>
        <p>请先在「数据导入」里导入财务数据，系统会自动计算每个期间的指标和预警。</p>
        <router-link class="btn" to="/financial/import">去导入数据</router-link>
      </section>

      <section v-else-if="outline" class="page-card">
        <div class="card-head">
          <div>
            <h2>{{ period }} 演示文稿</h2>
            <p class="muted">
              来源：<code>GET /api/v1/reports/pptx?period={{ period }}</code
              >； 共 {{ outline.slides.length }} 页，文件名 {{ outline.filename }}。
            </p>
          </div>
          <div class="actions">
            <button class="btn primary" :disabled="downloading" @click="download">
              {{ downloading ? '生成中…' : '下载 .pptx' }}
            </button>
          </div>
        </div>

        <section v-if="!outline.has_data" class="state-box notice">
          <strong>该期间没有指标数据</strong>
          <p>导出的演示文稿只有封面与说明页，请先导入数据并重算指标。</p>
        </section>

        <p v-if="saved" class="state-box notice">{{ saved }}</p>

        <ol class="outline-list">
          <li v-for="(slide, index) in outline.slides" :key="index">
            <div class="outline-head">
              <strong>第 {{ index + 1 }} 页 · {{ slide.title }}</strong>
              <span class="level">{{ KIND_LABEL[slide.kind] || slide.kind }}</span>
            </div>
            <p v-if="slide.subtitle" class="muted">{{ slide.subtitle }}</p>
            <ul v-if="slide.table?.length" class="outline-table">
              <li v-for="([label, value], i) in slide.table" :key="i">
                <span class="muted">{{ label }}</span> <strong>{{ value }}</strong>
              </li>
            </ul>
            <ul v-else-if="slide.bullets.length">
              <li v-for="(line, i) in slide.bullets" :key="i">{{ line }}</li>
            </ul>
            <p v-else-if="slide.kind === 'chart'" class="muted">
              费用构成用 python-pptx 原生柱状图绘制，不依赖 matplotlib 截图。
            </p>
          </li>
        </ol>
      </section>
    </main>
  </div>
</template>
