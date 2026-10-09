<template>
  <div class="export-menu">
    <button class="btn" :disabled="busy" @click="download('csv')">{{ buttonLabel('CSV') }}</button>
    <button class="btn" :disabled="busy" @click="download('xlsx')">{{ buttonLabel('Excel') }}</button>
    <small v-if="message" :class="['export-msg', failed && 'bad']">{{ message }}</small>
  </div>
</template>

<script setup>
/**
 * 导出按钮组：走后端 `/api/v1/export/*`，不在前端拼数据。
 *
 * 两个有意为之的细节：
 * 1. 文件名与行数都取自响应头（`Content-Disposition` / `X-Export-Rows`），因为**导出的行数由服务端
 *    的权限与上限决定**；前端自己数一遍没有意义，反而会掩盖「被 20000 行上限截断」这件事。
 * 2. 金额、期间这些「看起来是数字」的列，XLSX 里是真数字（能直接求和），CSV 里保持文本
 *    （避免 Excel 把 `2026-10` 认成日期、把部门号 `097` 变成 `97`）。
 */
import { ref } from 'vue'
import { apiFetch } from '../api/client'
import { withScope } from '../api/scope'
import { saveBlob } from '../api/download'

const props = defineProps({
  kind: { type: String, required: true }, // transactions | metrics | alerts
  period: { type: String, default: '' },
  periodFrom: { type: String, default: '' },
  periodTo: { type: String, default: '' },
  // 指标导出的粒度（只对 kind="metrics" 有效）：月行在 financial_metrics，季/年合计在 financial_rollups，
  // 所以「导出季度」和「导出月度」是两个请求（⑲）。
  grain: { type: String, default: '' },
  // 按钮前缀：同一页出现两个导出按钮时靠它区分（默认「导出」）。
  label: { type: String, default: '' },
  // 页面自己的口径筛选（明细页的「口径」下拉）。给了就以它为准，见 `query()`。
  department: { type: String, default: '' },
  // 明细页的批次与关键字筛选：与列表走同一套后端筛选，页面上筛完导出的就是屏幕上那一批。
  batch: { type: String, default: '' },
  keyword: { type: String, default: '' },
  account: { type: String, default: '' },
  level: { type: String, default: '' }
})

const busy = ref(false)
const failed = ref(false)
const message = ref('')

function buttonLabel(name) {
  return `${props.label || '导出'} ${name}`
}

function query(format) {
  const params = new URLSearchParams()
  params.set('format', format)
  if (props.period) params.set('period', props.period)
  if (props.periodFrom) params.set('period_from', props.periodFrom)
  if (props.periodTo) params.set('period_to', props.periodTo)
  if (props.grain && props.kind === 'metrics') params.set('grain', props.grain)
  if (props.department) params.set('department', props.department)
  if (props.batch) params.set('batch', props.batch)
  if (props.keyword) params.set('query', props.keyword)
  if (props.account) params.set('account', props.account)
  if (props.level) params.set('level', props.level)
  const path = `/export/${props.kind}?${params.toString()}`
  // 页面自带口径时不叠加全局口径：`withScope` 会再拼一个 `department`，两个同名参数的先后
  // 由解析顺序决定，与其猜不如让页面上看得见的那个（它就在旁边）说了算。
  return props.department ? path : withScope(path)
}

function filenameFrom(response, format) {
  const disposition = response.headers.get('Content-Disposition') || ''
  // 中文文件名走 RFC 5987 的 filename*（HTTP 头只能 latin-1），必须先读它，
  // 否则会拿到后端为兼容老浏览器准备的 ASCII 兜底名（中文被剔掉了）。
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1])
    } catch {
      // 编码坏了就退回 ASCII 名
    }
  }
  const match = /filename="?([^";]+)"?/i.exec(disposition)
  if (match && match[1] && !match[1].toLowerCase().startsWith('utf-8')) return match[1]
  // 兜底名：服务端没给 Content-Disposition 时用，粒度要带上，否则「导出季度」和「导出月度」重名
  const grain = props.kind === 'metrics' && props.grain ? `-${props.grain}` : ''
  return `${props.kind}${grain}-${props.period || props.periodFrom || 'all'}.${format}`
}

async function download(format) {
  busy.value = true
  failed.value = false
  message.value = ''
  try {
    const response = await apiFetch(query(format))
    const blob = await response.blob()
    const name = filenameFrom(response, format)
    const rows = response.headers.get('X-Export-Rows')
    const truncated = response.headers.get('X-Export-Truncated') === 'true'
    saveBlob(blob, name)
    const size = blob.size >= 1024 ? `${Math.round(blob.size / 1024)} KB` : `${blob.size} B`
    message.value = rows
      ? `已保存 ${name}（${rows} 行${truncated ? '，已达上限被截断' : ''}，${size}）`
      : `已保存 ${name}（${size}）`
  } catch (error) {
    failed.value = true
    message.value = `导出失败：${error.message}`
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.export-menu {
  /* inline-flex 而不是 flex：明细页的筛选区是**行内流**（`.filter-card` 本身没有样式，
     里面的 label/select/button 排在一行），块级的 flex 会强行换行、把表格整体往下顶一行。
     放进 `.actions` / `.card-actions` 这类 flex 容器时，inline-flex 会被块级化成 flex，外观不变。 */
  display: inline-flex;
  vertical-align: middle;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.export-msg {
  font-size: 12px;
  color: #52c41a;
}
.export-msg.bad {
  color: #ff4d4f;
}
</style>
