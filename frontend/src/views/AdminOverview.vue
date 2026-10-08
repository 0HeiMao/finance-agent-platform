<script setup>
import { computed, onMounted, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const sidebarOpen = ref(false)
const data = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/overview')
    data.value = await response.json()
  } catch (e) {
    error.value = `读取管理概览失败：${e.message}`
    data.value = null
  } finally {
    loading.value = false
  }
}

const COUNT_LABELS = [
  ['users', '用户', '人'],
  ['admins', '其中管理员', '人'],
  ['transactions', '交易明细', '行'],
  ['metrics', '期间指标', '期'],
  ['alerts', '预警', '条'],
  ['knowledge_chunks', '知识库片段', '条'],
  ['knowledge_sources', '知识库来源', '份'],
  ['batches', '导入批次', '个'],
]

// 只看最慢的几条路径：全局 P95 说明整体感受，路径 P95 才指出该去优化谁。
const SLOW_PATH_LIMIT = 5
const slowPaths = computed(() => {
  const paths = data.value?.observability?.paths || {}
  return Object.entries(paths)
    .map(([path, stat]) => ({ path, ...stat }))
    .sort((a, b) => b.p95_ms - a.p95_ms)
    .slice(0, SLOW_PATH_LIMIT)
})

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
            <h1>管理概览</h1>
            <p>数据规模、完整性体检与接口运行观测；只有不受部门限制的账号可以进入。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '读取中…' : '↻ 重新读取' }}</button>
        </div>
      </header>

      <p v-if="loading && !data" class="muted">正在读取管理概览…</p>

      <section v-else-if="error && !data" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else-if="data">
        <section v-if="error" class="state-box error-state"><strong>操作未完成</strong><p>{{ error }}</p></section>

        <section class="page-card">
          <div class="card-head">
            <h2>数据规模</h2>
            <span>截止本次读取，全部来自数据库实时计数</span>
          </div>
          <div class="settings-grid">
            <div v-for="[key, label, unit] in COUNT_LABELS" :key="key">
              <span>{{ label }}</span>
              <strong>{{ data.counts[key] }} {{ unit }}</strong>
            </div>
            <div>
              <span>最新期间</span>
              <strong>{{ data.periods.latest || '（无）' }}</strong>
              <small>{{ data.periods.all.length }} 个可用期间</small>
            </div>
            <div>
              <span>当前账号</span>
              <strong>{{ data.me.username }}</strong>
              <small>id {{ data.me.id }} · 不受部门限制</small>
            </div>
          </div>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>数据完整性体检</h2>
            <span>指标与明细对不上、口径字段缺失这类问题一律先亮出来</span>
          </div>
          <ul class="dir-list">
            <li v-for="check in data.quality" :key="check.name">
              <div class="dir-head">
                <strong>{{ check.name }}</strong>
                <span class="level" :class="check.level">{{ check.value }}</span>
              </div>
              <small class="quality-hint" :class="{ bad: check.level !== 'ok' }">{{ check.hint }}</small>
            </li>
          </ul>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>最近导入批次</h2>
            <router-link class="link" to="/admin/batches">去批次管理 →</router-link>
          </div>
          <p v-if="!data.recent_batches.length" class="muted">还没有任何导入批次。</p>
          <ul v-else class="dir-list">
            <li v-for="batch in data.recent_batches" :key="batch.batch">
              <div class="dir-head">
                <strong>{{ batch.short }}…</strong>
                <span class="level low">{{ batch.rows }} 行</span>
              </div>
              <small class="quality-hint">{{ batch.first_date }} ~ {{ batch.last_date }} ·
                {{ batch.periods.length }} 个期间 · 导入于 {{ batch.imported_at }}</small>
            </li>
          </ul>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>接口运行观测</h2>
            <span>进程内计数，重启即清零</span>
          </div>
          <div class="settings-grid">
            <div>
              <span>累计请求</span>
              <strong>{{ data.observability.total }} 次</strong>
              <small>按状态码与路径分桶 {{ Object.keys(data.observability.requests).length }} 个</small>
            </div>
            <div>
              <span>P95 响应耗时</span>
              <strong>{{ data.observability.p95_duration_ms }} ms</strong>
              <small>仅统计最近 1000 次采样</small>
            </div>
          </div>
          <h3 class="sub-head">最慢的接口（按路径 P95）</h3>
          <p v-if="!slowPaths.length" class="muted">本次进程还没有请求记录。</p>
          <ul v-else class="dir-list">
            <li v-for="row in slowPaths" :key="row.path">
              <div class="dir-head">
                <strong class="path-text">{{ row.path }}</strong>
                <span class="level" :class="row.p95_ms >= 500 ? 'high' : 'low'">{{ row.p95_ms }} ms</span>
              </div>
              <small class="quality-hint">采样 {{ row.count }} 次 · 最慢 {{ row.max_ms }} ms</small>
            </li>
          </ul>
          <p class="quality-hint">每路径只保留最近 {{ data.observability.samples_per_path }} 次采样；进程重启即清零。</p>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>管理入口</h2></div>
          <ul class="dir-list">
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/users">用户管理</router-link></strong>
                <span class="level low">{{ data.counts.users }} 人</span>
              </div>
              <small class="quality-hint">改部门＝改数据可见范围；部门为空即管理员</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/departments">部门权限</router-link></strong>
              </div>
              <small class="quality-hint">部门决定财务数据与知识库的可见范围；部门为空即不受限</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/alert-rules">预警规则</router-link></strong>
              </div>
              <small class="quality-hint">阈值与开关存在库里，改完在下一次重算（导入/撤销批次）时生效</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/audit">审计日志</router-link></strong>
              </div>
              <small class="quality-hint">导入、撤销批次、删账号、删知识库、改规则、处理预警、导出都会留痕</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/batches">导入批次</router-link></strong>
                <span class="level low">{{ data.counts.batches }} 个</span>
              </div>
              <small class="quality-hint">撤销批次会同步重算受影响期间，不会留下对不上的历史指标</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/admin/knowledge">知识库管理</router-link></strong>
                <span class="level low">{{ data.counts.knowledge_chunks }} 条</span>
              </div>
              <small class="quality-hint">按来源整份查看与删除，上传接口本身按内容哈希去重</small>
            </li>
            <li>
              <div class="dir-head">
                <strong><router-link class="link" to="/settings">System 设置</router-link></strong>
              </div>
              <small class="quality-hint">配置体检、目录可写性、模型与检索口径</small>
            </li>
          </ul>
        </section>
      </template>
    </main>
  </div>
</template>
