<script setup>
import { onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'
import { formatTime } from '../utils/format'

const PAGE_SIZE = 50

const sidebarOpen = ref(false)
const rows = ref([])
const total = ref(0)
const actions = ref([])
const actors = ref([])
const ips = ref([])
const offset = ref(0)
const actionFilter = ref('')
const actorFilter = ref('')
const ipFilter = ref('')
const sinceFilter = ref('')
const untilFilter = ref('')
const retentionDays = ref(180)
const exportLimit = ref(5000)
const keepDays = ref(180)
const purgePreview = ref(null)
const exporting = ref(false)
const purging = ref(false)
const notice = ref('')
const loading = ref(false)
const error = ref('')
const loaded = ref(false)

// 筛选条件既给列表用也给导出用：导出的必须是「我看到的这一批」，不是整张表
function filters() {
  const query = new URLSearchParams()
  if (actionFilter.value) query.set('action', actionFilter.value)
  if (actorFilter.value) query.set('actor', actorFilter.value)
  if (ipFilter.value) query.set('ip', ipFilter.value)
  if (sinceFilter.value) query.set('since', sinceFilter.value)
  if (untilFilter.value) query.set('until', untilFilter.value)
  return query
}

async function load() {
  loading.value = true
  error.value = ''
  const query = filters()
  query.set('limit', String(PAGE_SIZE))
  query.set('offset', String(offset.value))
  try {
    const response = await apiFetch(`/admin/audit?${query.toString()}`)
    const payload = await response.json()
    rows.value = payload.data || []
    total.value = payload.total || 0
    if (!actions.value.length) actions.value = payload.actions || []
    actors.value = payload.actors || []
    ips.value = payload.ips || []
    if (payload.retention_days) {
      retentionDays.value = payload.retention_days
      if (!keepDays.value) keepDays.value = payload.retention_days
    }
    if (payload.export_limit) exportLimit.value = payload.export_limit
  } catch (err) {
    rows.value = []
    total.value = 0
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

function reload() {
  offset.value = 0
  purgePreview.value = null
  load()
}

function turn(next) {
  offset.value = next
  load()
}

// 导出走 fetch + blob 而不是 <a href>：接口要 Bearer 令牌，直接开新窗口会 401。
// 文件名与后端 Content-Disposition 用同一种时间戳格式，下载列表里好认。
async function exportCsv() {
  exporting.value = true
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/admin/audit/export?${filters().toString()}`)
    const blob = await response.blob()
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `audit-${stamp}.csv`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    const exported = response.headers.get('X-Export-Rows') || '0'
    const truncated = response.headers.get('X-Export-Truncated') === 'true'
    notice.value = truncated
      ? `已导出前 ${exported} 条（命中的更多，单次上限 ${exportLimit.value} 条——按时间或动作分段再导一次）`
      : `已导出 ${exported} 条审计记录（CSV，带 BOM，Excel 打开中文不乱码）`
    await load()
  } catch (err) {
    error.value = err && err.message ? err.message : '导出失败'
  } finally {
    exporting.value = false
  }
}

// 清理刻意做成两步：先预览会删多少条，再确认。审计的删除不该是一下点完的事
async function previewPurge() {
  purging.value = true
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch('/admin/audit/purge', {
      method: 'POST',
      body: JSON.stringify({ keep_days: Number(keepDays.value), dry_run: true })
    })
    purgePreview.value = await response.json()
  } catch (err) {
    purgePreview.value = null
    error.value = err && err.message ? err.message : '预览失败'
  } finally {
    purging.value = false
  }
}

async function confirmPurge() {
  purging.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/audit/purge', {
      method: 'POST',
      body: JSON.stringify({ keep_days: Number(keepDays.value), dry_run: false })
    })
    const payload = await response.json()
    purgePreview.value = null
    notice.value = payload.deleted
      ? `已清理 ${payload.deleted} 条早于 ${String(payload.before).slice(0, 10)} 的记录（保留 ${payload.keep_days} 天）；这次清理本身也写进审计`
      : `保留 ${payload.keep_days} 天范围内没有过期记录，未删除任何数据`
    await load()
  } catch (err) {
    error.value = err && err.message ? err.message : '清理失败'
  } finally {
    purging.value = false
  }
}

// 详情是「结构化事实」的字典，直接铺成 key=value 文本；空对象显示占位符而不是空白格
function detailText(detail) {
  const entries = Object.entries(detail || {})
  if (!entries.length) return '—'
  return entries
    .map(([key, value]) => `${key}=${value && typeof value === 'object' ? JSON.stringify(value) : value}`)
    .join(' · ')
}

watch([actionFilter, actorFilter, ipFilter, sinceFilter, untilFilter], reload)
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
            <h1>审计日志</h1>
            <p>谁在什么时候改了什么：导入、撤销批次、删账号、删知识库、改规则、处理预警、导出都会留痕。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="exporting || !total" @click="exportCsv">
            {{ exporting ? '导出中…' : '导出 CSV' }}
          </button>
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '加载中…' : '↻ 重新读取' }}
          </button>
          <router-link class="btn" to="/admin">返回管理概览</router-link>
        </div>
      </header>

      <section class="page-card">
        <div class="filter-row">
          <label class="field">
            <span>动作</span>
            <select v-model="actionFilter" class="select">
              <option value="">全部动作</option>
              <option v-for="item in actions" :key="item.value" :value="item.value">{{ item.label }}</option>
            </select>
          </label>
          <label class="field">
            <span>操作人</span>
            <select v-model="actorFilter" class="select">
              <option value="">全部操作人</option>
              <option v-for="name in actors" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
          <label class="field">
            <span>来源 IP</span>
            <select v-model="ipFilter" class="select">
              <option value="">全部 IP</option>
              <option v-for="address in ips" :key="address" :value="address">{{ address }}</option>
            </select>
          </label>
          <label class="field">
            <span>从</span>
            <input v-model="sinceFilter" class="select" type="date" />
          </label>
          <label class="field">
            <span>到</span>
            <input v-model="untilFilter" class="select" type="date" />
          </label>
          <span class="muted">命中 {{ total }} 条（含首尾两天，按业务时区的日历日）</span>
        </div>

        <p v-if="notice" class="notice">{{ notice }}</p>

        <section v-if="error" class="state-box error-state">
          <strong>日志加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="load">重试</button>
        </section>

        <div v-else-if="loading && !rows.length" class="muted">正在加载…</div>

        <section v-else-if="!rows.length" class="state-box empty-state">
          <strong>没有符合条件的记录</strong>
          <p>换个动作、操作人、IP 或时间范围看看；也可能是这段时间还没人做过写操作。</p>
        </section>

        <template v-else>
          <div class="table-wrap">
            <table class="audit-table">
              <thead>
                <tr>
                  <th>时间（本机）</th>
                  <th>操作人</th>
                  <th>动作</th>
                  <th>对象</th>
                  <th>详情</th>
                  <th>来源 IP</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in rows" :key="row.id">
                  <!-- 接口给的是 UTC（导出的 CSV 也保持 UTC 便于对数），这一列按浏览器本机时间显示。
                       注意「从/到」是按业务时区（BUSINESS_TIMEZONE）的日历日切的，与这一列口径不同：
                       筛选边界是账本口径、行内时刻是看表人口径，两个都显式标出来，别让人猜。 -->
                  <td class="nowrap">{{ formatTime(row.at) }}</td>
                  <td>
                    <strong>{{ row.actor }}</strong>
                    <small class="muted">{{ row.department || '不受限' }}</small>
                  </td>
                  <td>
                    <span class="action-chip">{{ row.action_label }}</span>
                    <small class="muted">{{ row.action }}</small>
                  </td>
                  <td>
                    <small class="muted">{{ row.target_type || '—' }}</small>
                    <span class="target">{{ row.target_id || '—' }}</span>
                  </td>
                  <!-- 详情列会按 CSS 截断，完整内容放 title：审计的价值就在细节，不能被省略号吃掉 -->
                  <td class="detail" :title="detailText(row.detail)">{{ detailText(row.detail) }}</td>
                  <td class="nowrap muted">{{ row.ip || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <PagePager :total="total" :limit="PAGE_SIZE" :offset="offset" :loading="loading" @change="turn" />
        </template>

        <p class="muted audit-hint">
          日志只增不减：没有任何「清空审计」的入口，唯一的删除是按保留期清理（默认 {{ retentionDays }} 天，
          分预览与确认两步），而且那次清理自己也会写一条 <code>audit.purge</code>。导出是
          <code>audit.export</code>，同样留痕——密码、令牌、上传文件内容一律不入库，详情里只有条数、
          批次号、旧值/新值这类结构化事实。
        </p>
      </section>

      <section class="page-card">
        <div class="card-head">
          <div>
            <h2>按保留期清理</h2>
            <p class="muted">
              只删「早于 N 天」的记录，N 至少为 1；先预览条数，再确认。清理动作本身写进审计，所以清理之后
              仍能查到「谁在什么时候删掉了多少条」。
            </p>
          </div>
        </div>
        <div class="filter-row">
          <label class="field">
            <span>保留天数</span>
            <input v-model="keepDays" class="select days-input" type="number" min="1" max="3650" />
          </label>
          <button class="btn" :disabled="purging" @click="previewPurge">
            {{ purging ? '处理中…' : '预览将清理的条数' }}
          </button>
          <button v-if="purgePreview" class="btn danger" :disabled="purging" @click="confirmPurge">
            确认清理 {{ purgePreview.deleted }} 条
          </button>
        </div>
        <p v-if="purgePreview" class="muted">
          预览（未删除）：按保留 <strong>{{ purgePreview.keep_days }}</strong> 天计算，截止
          {{ formatTime(purgePreview.before) }}（本机时间）之前有
          <strong>{{ purgePreview.deleted }}</strong> 条会被删除，库里共 {{ purgePreview.total }} 条。
        </p>
      </section>
    </main>
  </div>
</template>

<style scoped>
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
  margin-bottom: 14px;
}
.field {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.field span {
  color: #64748b;
}
.select {
  padding: 6px 8px;
  border: 1px solid #d7dee8;
  border-radius: 7px;
  background: #fff;
  color: inherit;
}
.days-input {
  width: 90px;
}
.notice {
  margin: 0 0 12px;
  padding: 8px 12px;
  border-radius: 8px;
  background: #f6ffed;
  color: #237804;
  font-size: 13px;
}
.card-head {
  margin-bottom: 12px;
}
.card-head h2 {
  margin: 0 0 4px;
  font-size: 15px;
}
.card-head p {
  margin: 0;
  font-size: 12px;
}
.btn.danger {
  border-color: #ffa39e;
  color: #a8071a;
}
.audit-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 980px;
}
.audit-table th {
  text-align: left;
  font-size: 13px;
  color: #64748b;
  font-weight: 500;
  padding: 6px 10px 6px 0;
}
.audit-table td {
  padding: 8px 10px 8px 0;
  vertical-align: top;
  border-top: 1px solid #eef1f6;
  font-size: 13px;
}
.audit-table small {
  display: block;
  font-size: 11px;
}
.nowrap {
  white-space: nowrap;
}
.target {
  display: block;
  font-family: Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}
.detail {
  max-width: 420px;
  color: #475569;
  overflow-wrap: anywhere;
}
.action-chip {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef3f9;
  color: #1a3a5c;
  font-size: 12px;
}
.audit-hint {
  margin-top: 14px;
  font-size: 12px;
}
.audit-hint code {
  font-family: Consolas, monospace;
  font-size: 12px;
}
</style>
