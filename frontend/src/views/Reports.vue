<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import { saveBlob } from '../api/download'
import { activeScope, withScope } from '../api/scope'
import AppSidebar from '../components/AppSidebar.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import { renderMarkdown } from '../utils/markdown'

const sidebarOpen = ref(false)
const periods = ref([])
const period = ref('')
const markdown = ref('')
const loading = ref(false)
const error = ref('')
const clipboardError = ref(false)
const copied = ref(false)
const savingDocx = ref(false)
const docxMessage = ref('')

const isPlaceholder = computed(() => markdown.value.includes('数据不足'))

// 报告内容全部由后端生成（不含用户输入）。renderMarkdown 内部先做 HTML 转义再解析结构，
// 所以即便后端未来拼进外部文本，v-html 拿到的也只是我们自己生成的标签。
const html = computed(() => renderMarkdown(markdown.value))

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

async function loadReport() {
  if (!period.value) return
  loading.value = true
  error.value = ''
  clipboardError.value = false
  copied.value = false
  try {
    const r = await apiFetch(withScope(`/reports/monthly?period=${encodeURIComponent(period.value)}`))
    markdown.value = await r.text()
  } catch (e) {
    error.value = `生成报告失败：${e.message}`
    markdown.value = ''
  } finally {
    loading.value = false
  }
}

function download() {
  if (!markdown.value) return
  const blob = new Blob([markdown.value], { type: 'text/markdown;charset=utf-8' })
  saveBlob(blob, `财务分析报告-${period.value}.md`)
}

// Word 报告由后端用 python-docx 生成（表格里的数字与页面同源），前端只负责保存。
// 文件名在前端定：响应头里虽然有 filename*，但保存名以这里传入的为准，口径写在名字上
// 才不会出现「部门报告被当成公司报告发出去」。
async function downloadDocx() {
  if (!period.value) return
  savingDocx.value = true
  docxMessage.value = ''
  try {
    const r = await apiFetch(withScope(`/reports/docx?period=${encodeURIComponent(period.value)}`))
    const blob = await r.blob()
    const tag = activeScope.value ? `-${activeScope.value}` : ''
    saveBlob(blob, `财务分析报告-${period.value}${tag}.docx`)
    docxMessage.value = `已保存 财务分析报告-${period.value}${tag}.docx（${Math.max(1, Math.round(blob.size / 1024))} KB）`
  } catch (e) {
    error.value = `生成 Word 报告失败：${e.message}`
  } finally {
    savingDocx.value = false
  }
}

async function copy() {
  if (!markdown.value) return
  try {
    await navigator.clipboard.writeText(markdown.value)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch {
    clipboardError.value = true
    error.value = '浏览器拒绝了剪贴板访问（无头/非安全上下文常见），请改用「下载 Markdown」。'
  }
}

function printReport() {
  window.print()
}

onMounted(async () => {
  await loadPeriods()
  await loadReport()
})
// 切口径后报告必须重新生成：报告是要外发的文件，口径错了比页面上看错更严重。
watch(activeScope, loadReport)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>报告导出</h1>
            <p>按期间生成 Word（.docx）与 Markdown 财务分析报告，指标与口径说明随文档一起下发。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
          <select v-model="period" class="select" :disabled="!periods.length" @change="loadReport">
            <option v-if="!periods.length" value="">暂无可用期间</option>
            <option v-for="p in periods" :key="p" :value="p">{{ p }}</option>
          </select>
          <button class="btn" :disabled="loading || !period" @click="loadReport">{{ loading ? '生成中…' : '↻ 重新生成' }}</button>
        </div>
      </header>

      <p v-if="loading" class="muted">正在生成 {{ period }} 的报告…</p>

      <section v-else-if="error" class="state-box" :class="clipboardError ? 'notice' : 'error-state'">
        <strong>{{ clipboardError ? '复制失败' : '生成失败' }}</strong>
        <p>{{ error }}</p>
        <button class="btn" v-if="!clipboardError" @click="loadReport">重试</button>
      </section>

      <section v-else-if="!periods.length" class="state-box empty-state">
        <strong>还没有可导出的期间</strong>
        <p>请先在「数据导入」里导入财务数据，系统会自动计算每个期间的指标和预警。</p>
        <router-link class="btn" to="/financial/import">去导入数据</router-link>
      </section>

      <section v-else class="page-card">
        <div class="card-head">
          <div>
            <h2>{{ period }} 财务分析报告</h2>
            <p class="muted">来源：<code>GET /api/v1/reports/monthly?period={{ period }}</code>；金额与比率由程序确定性计算，报告只做呈现。</p>
          </div>
          <div class="actions">
            <button class="btn" @click="copy">{{ copied ? '已复制' : '复制 Markdown' }}</button>
            <button class="btn" @click="printReport">打印 / 导出 PDF</button>
            <button class="btn" :disabled="isPlaceholder" @click="download">下载 Markdown</button>
            <button class="btn primary" :disabled="isPlaceholder || savingDocx" @click="downloadDocx">
              {{ savingDocx ? '生成中…' : '下载 Word (.docx)' }}
            </button>
          </div>
        </div>

        <p v-if="docxMessage" class="muted" style="margin:-6px 0 12px">{{ docxMessage }}</p>

        <section v-if="isPlaceholder" class="state-box notice">
          <strong>该期间没有指标数据</strong>
          <p>报告接口返回的是占位内容，不包含数字，因此不提供下载。</p>
        </section>

        <div class="report-body" v-html="html" />
      </section>
    </main>
  </div>
</template>
