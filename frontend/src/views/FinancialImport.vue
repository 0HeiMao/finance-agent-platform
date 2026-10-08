<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client'
import { saveBlob } from '../api/download'
import AppSidebar from '../components/AppSidebar.vue'

const file = ref(null); const loading = ref(false); const previewing = ref(false)
const result = ref(null); const preview = ref(null); const error = ref(''); const sidebarOpen = ref(false)

function choose(event) { file.value = event.target.files[0]; preview.value = null; result.value = null; error.value = '' }
async function downloadTemplate() {
  try {
    const r = await apiFetch('/financial/template')
    const blob = await r.blob()
    saveBlob(blob, 'financial_import_template.csv')
  } catch (e) { error.value = `模板下载失败：${e.message}`; ElMessage.error(e.message) }
}
async function previewFile() {
  if (!file.value) return ElMessage.warning('请先选择文件')
  preview.value = null; error.value = ''; previewing.value = true
  try {
    const body = new FormData(); body.append('file', file.value)
    const r = await apiFetch('/financial/preview', { method: 'POST', body, headers: {} })
    preview.value = await r.json()
  } catch (e) { error.value = `预览失败：${e.message}`; ElMessage.error(e.message) } finally { previewing.value = false }
}
function downloadErrors() {
  // 错误清单直接由预览响应拼 CSV：错误是「这一行哪里不合法」，后端已经逐行给全了，
  // 再让后端跑一次没有意义（而且预览按定义不落库）。加 BOM 让 Excel 双击打开不乱码。
  const rows = (preview.value?.errors || []).map((item) => [item.row, item.error])
  const csv = ['行号,问题', ...rows.map(([row, text]) => `${row},"${String(text).replace(/"/g, '""')}"`)].join('\r\n')
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
  saveBlob(new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' }), `import-errors-${stamp}.csv`)
  ElMessage.success(`已保存 ${rows.length} 条错误`)
}

async function upload() {
  if (!file.value || !preview.value?.valid || loading.value) return ElMessage.warning('请先通过预览校验')
  loading.value = true; error.value = ''
  try {
    const body = new FormData(); body.append('file', file.value)
    const r = await apiFetch('/financial/import', { method: 'POST', body, headers: {} })
    result.value = await r.json()
    if (result.value.success) ElMessage.success('导入完成')
    else { error.value = `校验未通过，未写入任何数据（${result.value.errors?.length || 0} 个错误行），请修正后重试。`; ElMessage.warning('校验未通过，未写入数据') }
  } catch (e) { error.value = `导入失败：${e.message}`; ElMessage.error(e.message) } finally { loading.value = false }
}
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>财务数据导入</h1>
            <p>先预览并校验，再写入数据库；同一文件重复提交会被拒绝。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" @click="downloadTemplate">下载导入模板</button>
          <router-link class="btn" to="/dashboard">返回看板</router-link>
        </div>
      </header>

      <section class="page-card">
        <div class="upload-box">
          <input type="file" accept=".xlsx,.xls,.csv" :disabled="loading || previewing" @change="choose">
          <button class="btn" :disabled="previewing" @click="previewFile">{{ previewing ? '校验中…' : '预览校验' }}</button>
          <button class="btn primary" :disabled="loading || previewing || !preview?.valid" @click="upload">{{ loading ? '处理中…' : '确认导入' }}</button>
        </div>

        <section v-if="error" class="state-box error-state">
          <strong>操作未完成</strong>
          <p>{{ error }}</p>
        </section>

        <div v-if="preview" class="result-box">
          共 {{ preview.total }} 行；字段：{{ preview.columns.join('、') }}
          <span v-if="preview.missing.length">；缺少：{{ preview.missing.join('、') }}</span>
          <span v-else>；必填字段完整</span>
          <div v-if="preview.errors.length">
            发现 {{ preview.error_count }} 个错误
            <button class="btn" @click="downloadErrors">下载错误清单</button>
            <ul><li v-for="item in preview.errors" :key="item.row">第 {{ item.row }} 行：{{ item.error }}</li></ul>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th v-for="column in preview.columns" :key="column">{{ column }}</th></tr></thead>
              <tbody>
                <tr v-for="(row, index) in preview.preview" :key="index">
                  <td v-for="column in preview.columns" :key="column">{{ row[column] }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="result" class="result-box">
          已导入 {{ result.imported }} 行，批次：{{ result.batch }}
          <div v-if="result.errors?.length">错误行：{{ result.errors.length }} 条</div>
          <div v-if="result.calculated?.length">已计算期间：{{ result.calculated.map((x) => x.period).join('、') }}</div>
        </div>

        <p><router-link to="/dashboard">返回看板并刷新数据</router-link></p>
      </section>
    </main>
  </div>
</template>
