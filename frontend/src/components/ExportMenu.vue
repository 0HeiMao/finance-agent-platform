<template>
  <div class="export-menu">
    <button class="btn" :disabled="busy" @click="download('csv')">导出 CSV</button>
    <button class="btn" :disabled="busy" @click="download('xlsx')">导出 Excel</button>
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
  kind: { type: String, required: true },      // transactions | metrics | alerts
  period: { type: String, default: '' },
  periodFrom: { type: String, default: '' },
  periodTo: { type: String, default: '' },
  account: { type: String, default: '' },
  level: { type: String, default: '' },
})

const busy = ref(false)
const failed = ref(false)
const message = ref('')

function query(format) {
  const params = new URLSearchParams()
  params.set('format', format)
  if (props.period) params.set('period', props.period)
  if (props.periodFrom) params.set('period_from', props.periodFrom)
  if (props.periodTo) params.set('period_to', props.periodTo)
  if (props.account) params.set('account', props.account)
  if (props.level) params.set('level', props.level)
  return withScope(`/export/${props.kind}?${params.toString()}`)
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
  if (match && match[1] && !match[1].toLowerCase().startsWith('utf-8'))
    return match[1]
  return `${props.kind}-${props.period || 'all'}.${format}`
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
.export-menu { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.export-msg { font-size: 12px; color: #52c41a; }
.export-msg.bad { color: #ff4d4f; }
</style>
