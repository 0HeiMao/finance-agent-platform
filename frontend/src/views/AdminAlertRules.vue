<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const sidebarOpen = ref(false)
const rules = ref([])
const metrics = ref({})
const comparators = ref({})
const levels = ref({})
const departments = ref([])
const periods = ref([])
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
const saving = ref(0)
const removing = ref(0)
const creating = ref(false)
const drafts = ref({})

// 分档规则让同一个 alert_type 能出现多条，所以草稿必须按 id 存：
// 按 alert_type 存的话「部门A 的 0.95」和「全期间的 0.05」会互相覆盖。
const form = reactive({
  alert_type: '成本率异常', department: '', period: '',
  metric_name: 'cost_ratio', comparator: 'gt', threshold_value: '0.80',
  level: 'high', require_metric: '', description: '', enabled: true,
})

const types = computed(() => [...new Set(rules.value.map((rule) => rule.alert_type))])
const defaults = computed(() => rules.value.filter((rule) => rule.is_default))
const tiers = computed(() => rules.value.filter((rule) => !rule.is_default))

function syncDrafts() {
  drafts.value = Object.fromEntries(rules.value.map((rule) => [rule.id, {
    threshold_value: rule.threshold_value,
    level: rule.level,
    enabled: rule.enabled,
    description: rule.description,
  }]))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/alert-rules')
    const payload = await response.json()
    rules.value = payload.data || []
    metrics.value = payload.metrics || {}
    comparators.value = payload.comparators || {}
    levels.value = payload.levels || {}
    departments.value = payload.departments || []
    periods.value = payload.periods || []
    syncDrafts()
  } catch (err) {
    rules.value = []
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

async function save(rule) {
  const draft = drafts.value[rule.id]
  saving.value = rule.id
  try {
    // 一律走 id：默认规则与分档规则用同一个端点，页面上不必记「这条该用哪个 URL」
    const response = await apiFetch(`/admin/alert-rules/id/${rule.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        threshold_value: draft.threshold_value,
        level: draft.level,
        enabled: draft.enabled,
        description: draft.description,
      }),
    })
    const updated = await response.json()
    Object.assign(rule, updated)
    drafts.value[rule.id].threshold_value = updated.threshold_value
    ElMessage.success(`「${rule.alert_type}」${rule.scope_label} 已保存，下次重算生效`)
  } catch (err) {
    ElMessage.error(err && err.message ? err.message : '保存失败')
  } finally {
    saving.value = 0
  }
}

async function remove(rule) {
  if (!window.confirm(`删除「${rule.alert_type}」（${rule.scope_label}）？删除后该范围回到更宽的规则。`)) return
  removing.value = rule.id
  try {
    await apiFetch(`/admin/alert-rules/id/${rule.id}`, { method: 'DELETE' })
    ElMessage.success(`已删除「${rule.alert_type}」${rule.scope_label}`)
    await load()
  } catch (err) {
    ElMessage.error(err && err.message ? err.message : '删除失败')
  } finally {
    removing.value = 0
  }
}

async function create() {
  if (!form.department && !form.period) {
    ElMessage.warning('分档规则至少要限定部门或期间之一')
    return
  }
  creating.value = true
  try {
    await apiFetch('/admin/alert-rules', { method: 'POST', body: JSON.stringify({ ...form }) })
    ElMessage.success('分档规则已建立，重算后生效')
    form.department = ''
    form.period = ''
    await load()
  } catch (err) {
    ElMessage.error(err && err.message ? err.message : '新增失败')
  } finally {
    creating.value = false
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
            <h1>预警规则</h1>
            <p>阈值与开关放在库里，改完对下一次重算立即生效；已有预警不会被追溯改写。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '加载中…' : '↻ 重新读取' }}</button>
          <router-link class="btn" to="/admin">返回管理概览</router-link>
        </div>
      </header>

      <section class="page-card">
        <section v-if="error" class="state-box error-state">
          <strong>规则加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="load">重试</button>
        </section>

        <div v-else-if="loading && !rules.length" class="muted">正在加载…</div>

        <div v-else class="table-wrap">
          <table class="rule-table">
            <thead>
              <tr>
                <th>预警类型</th><th>适用范围</th><th>判定条件</th><th>阈值</th>
                <th>级别</th><th>启用</th><th>描述</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="rule in rules" :key="rule.id" :class="{ 'rule-default': rule.is_default }">
                <td>
                  <strong>{{ rule.alert_type }}</strong>
                  <small class="muted">{{ rule.metric_label }}</small>
                </td>
                <td>
                  <span class="tag" :class="rule.is_default ? 'tag-default' : 'tag-tier'">
                    {{ rule.is_default ? '默认' : '分档' }}
                  </span>
                  <small class="muted">{{ rule.scope_label }}</small>
                </td>
                <td class="muted">
                  {{ metrics[rule.metric_name] || rule.metric_name }} {{ rule.comparator_label }} 阈值
                  <small v-if="rule.require_metric" class="muted">
                    （{{ metrics[rule.require_metric] || rule.require_metric }} 为 0 时不判定）
                  </small>
                </td>
                <td>
                  <input v-model="drafts[rule.id].threshold_value" class="input mini" type="text" inputmode="decimal" />
                  <small class="muted">当前 {{ rule.threshold_value }}</small>
                </td>
                <td>
                  <select v-model="drafts[rule.id].level" class="select mini">
                    <option value="high">高</option>
                    <option value="medium">中</option>
                    <option value="low">低</option>
                  </select>
                </td>
                <td><input v-model="drafts[rule.id].enabled" type="checkbox" /></td>
                <td><input v-model="drafts[rule.id].description" class="input wide" type="text" /></td>
                <td>
                  <button class="btn" :disabled="saving === rule.id" @click="save(rule)">
                    {{ saving === rule.id ? '保存中…' : '保存' }}
                  </button>
                  <button class="btn danger" :disabled="rule.is_default || removing === rule.id"
                          :title="rule.is_default ? '默认规则只能停用，不能删除' : '删除这条分档规则'"
                          @click="remove(rule)">
                    {{ removing === rule.id ? '删除中…' : '删除' }}
                  </button>
                  <small class="muted">更新于 {{ rule.updated_at || '—' }}</small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p class="muted rule-hint">
          规则形状（比哪个指标、大于还是小于）不在这里改：平台只有「某指标超过/低于阈值」这一种真实需求，
          做成表达式编辑器反而无法验证。改小的阈值会让更多期间命中预警——重算期间用
          <code>POST /api/v1/analysis/calculate?period=YYYY-MM</code> 或重新导入同一文件。
        </p>
      </section>

      <section class="page-card">
        <h2>新增分档规则</h2>
        <p class="muted section-hint">
          同一个预警类型可以按<b>部门</b>、按<b>期间</b>各配一条阈值。重算时<b>取最具体的一条</b>：
          部门+期间 &gt; 只限一个维度 &gt; 默认规则；命中即覆盖，不会叠加。
          现在有 {{ defaults.length }} 条默认、{{ tiers.length }} 条分档。
        </p>

        <div class="form-grid">
          <label class="field">
            <span>预警类型</span>
            <select v-model="form.alert_type" class="select">
              <option v-for="type in types" :key="type" :value="type">{{ type }}</option>
            </select>
          </label>
          <label class="field">
            <span>指标</span>
            <select v-model="form.metric_name" class="select">
              <option v-for="(label, name) in metrics" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field">
            <span>判定</span>
            <select v-model="form.comparator" class="select">
              <option v-for="(label, name) in comparators" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field">
            <span>阈值</span>
            <input v-model="form.threshold_value" class="input" type="text" inputmode="decimal" placeholder="例如 0.95" />
          </label>
          <label class="field">
            <span>部门（可留空）</span>
            <select v-model="form.department" class="select">
              <option value="">不限定部门</option>
              <option v-for="name in departments" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
          <label class="field">
            <span>期间（可留空）</span>
            <select v-model="form.period" class="select">
              <option value="">不限定期间</option>
              <option v-for="period in periods" :key="period" :value="period">{{ period }}</option>
            </select>
          </label>
          <label class="field">
            <span>级别</span>
            <select v-model="form.level" class="select">
              <option v-for="(label, name) in levels" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field">
            <span>说明</span>
            <input v-model="form.description" class="input" type="text" placeholder="例如：部门A 本期从严" />
          </label>
        </div>

        <div class="form-actions">
          <button class="btn primary" :disabled="creating" @click="create">
            {{ creating ? '新增中…' : '新增分档规则' }}
          </button>
          <span class="muted">两条分档规则不能落在同一个「类型 + 部门 + 期间」上（后端会返回 409，请直接改那一条）。</span>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.rule-table { width: 100%; border-collapse: collapse; min-width: 980px; }
.rule-table th { text-align: left; font-size: 13px; color: #64748b; font-weight: 500; padding: 6px 10px 6px 0; }
.rule-table td { padding: 8px 10px 8px 0; vertical-align: top; border-top: 1px solid #eef1f6; }
.rule-table small { display: block; font-size: 11px; }
.rule-table tr.rule-default td { background: #f8fafc; }
.input.mini, .select.mini { width: 96px; }
.input.wide { width: 100%; min-width: 160px; }
.rule-hint { margin-top: 14px; font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 8px; margin-right: 6px; }
.tag-default { background: #e2e8f0; color: #475569; }
.tag-tier { background: #dbeafe; color: #1d4ed8; }
h2 { font-size: 16px; margin: 0 0 6px; }
.section-hint { font-size: 12px; margin: 0 0 12px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #475569; }
.form-actions { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.form-actions .muted { font-size: 12px; }
.btn.danger { border-color: #fecaca; color: #b91c1c; }
.btn.danger:disabled { opacity: .45; }
.btn.primary { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
</style>
