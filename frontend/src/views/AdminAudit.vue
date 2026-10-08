<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'

const PAGE_SIZE = 50

const sidebarOpen = ref(false)
const rows = ref([])
const total = ref(0)
const actions = ref([])
const actors = ref([])
const offset = ref(0)
const actionFilter = ref('')
const actorFilter = ref('')
const loading = ref(false)
const error = ref('')
const loaded = ref(false)

const page = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)

async function load() {
  loading.value = true
  error.value = ''
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
  if (actionFilter.value) query.set('action', actionFilter.value)
  if (actorFilter.value) query.set('actor', actorFilter.value)
  try {
    const response = await apiFetch(`/admin/audit?${query.toString()}`)
    const payload = await response.json()
    rows.value = payload.data || []
    total.value = payload.total || 0
    if (!actions.value.length) actions.value = payload.actions || []
    actors.value = payload.actors || []
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
  load()
}

function turn(next) {
  offset.value = next
  load()
}

// 详情是「结构化事实」的字典，直接铺成 key=value 文本；空对象显示占位符而不是空白格
function detailText(detail) {
  const entries = Object.entries(detail || {})
  if (!entries.length) return '—'
  return entries
    .map(([key, value]) => `${key}=${value && typeof value === 'object' ? JSON.stringify(value) : value}`)
    .join(' · ')
}

watch([actionFilter, actorFilter], reload)
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
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '↻ 重新读取' }}</button>
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
          <span class="muted">命中 {{ total }} 条</span>
        </div>

        <section v-if="error" class="state-box error-state">
          <strong>日志加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="load">重试</button>
        </section>

        <div v-else-if="loading && !rows.length" class="muted">正在加载…</div>

        <section v-else-if="!rows.length" class="state-box empty-state">
          <strong>没有符合条件的记录</strong>
          <p>换个动作或操作人看看；也可能是这段时间还没人做过写操作。</p>
        </section>

        <template v-else>
          <div class="table-wrap">
            <table class="audit-table">
              <thead>
                <tr>
                  <th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th><th>来源 IP</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in rows" :key="row.id">
                  <td class="nowrap">{{ row.at ? row.at.replace('T', ' ').slice(0, 19) : '—' }}</td>
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
          日志是只读的：接口不提供删除与导出，管理员也清不掉（要归档请直接读库）。密码、令牌、上传文件内容
          一律不入库——详情里只有条数、批次号、旧值/新值这类结构化事实。
        </p>
      </section>
    </main>
  </div>
</template>

<style scoped>
.filter-row { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-bottom: 14px; }
.field { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.field span { color: #64748b; }
.select { padding: 6px 8px; border: 1px solid #d7dee8; border-radius: 7px; background: #fff; }
.audit-table { width: 100%; border-collapse: collapse; min-width: 980px; }
.audit-table th { text-align: left; font-size: 13px; color: #64748b; font-weight: 500; padding: 6px 10px 6px 0; }
.audit-table td { padding: 8px 10px 8px 0; vertical-align: top; border-top: 1px solid #eef1f6; font-size: 13px; }
.audit-table small { display: block; font-size: 11px; }
.nowrap { white-space: nowrap; }
.target { display: block; font-family: Consolas, monospace; font-size: 12px; word-break: break-all; }
.detail { max-width: 420px; color: #475569; overflow-wrap: anywhere; }
.action-chip { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef3f9; color: #1a3a5c; font-size: 12px; }
.audit-hint { margin-top: 14px; font-size: 12px; }
</style>
