<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const sidebarOpen = ref(false)
const rules = ref([])
const metrics = ref({})
const loading = ref(false)
const error = ref('')
const loaded = ref(false)
const savingType = ref('')
const drafts = ref({})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/alert-rules')
    const payload = await response.json()
    rules.value = payload.data || []
    metrics.value = payload.metrics || {}
    drafts.value = Object.fromEntries(rules.value.map((rule) => [rule.alert_type, {
      threshold_value: rule.threshold_value,
      level: rule.level,
      enabled: rule.enabled,
      description: rule.description,
    }]))
  } catch (err) {
    rules.value = []
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

async function save(rule) {
  const draft = drafts.value[rule.alert_type]
  savingType.value = rule.alert_type
  try {
    const response = await apiFetch(`/admin/alert-rules/${encodeURIComponent(rule.alert_type)}`, {
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
    drafts.value[rule.alert_type].threshold_value = updated.threshold_value
    ElMessage.success(`「${rule.alert_type}」已保存，下次重算生效`)
  } catch (err) {
    ElMessage.error(err && err.message ? err.message : '保存失败')
  } finally {
    savingType.value = ''
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
                <th>预警类型</th><th>判定条件</th><th>阈值</th><th>级别</th><th>启用</th><th>描述</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="rule in rules" :key="rule.alert_type">
                <td>
                  <strong>{{ rule.alert_type }}</strong>
                  <small class="muted">{{ rule.metric_label }}</small>
                </td>
                <td class="muted">
                  {{ metrics[rule.metric_name] || rule.metric_name }} {{ rule.comparator_label }} 阈值
                  <small v-if="rule.require_metric" class="muted">
                    （{{ metrics[rule.require_metric] || rule.require_metric }} 为 0 时不判定）
                  </small>
                </td>
                <td>
                  <input v-model="drafts[rule.alert_type].threshold_value" class="input mini" type="text" inputmode="decimal" />
                  <small class="muted">当前 {{ rule.threshold_value }}</small>
                </td>
                <td>
                  <select v-model="drafts[rule.alert_type].level" class="select mini">
                    <option value="high">高</option>
                    <option value="medium">中</option>
                    <option value="low">低</option>
                  </select>
                </td>
                <td><input v-model="drafts[rule.alert_type].enabled" type="checkbox" /></td>
                <td><input v-model="drafts[rule.alert_type].description" class="input wide" type="text" /></td>
                <td>
                  <button class="btn" :disabled="savingType === rule.alert_type" @click="save(rule)">
                    {{ savingType === rule.alert_type ? '保存中…' : '保存' }}
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
    </main>
  </div>
</template>

<style scoped>
.rule-table { width: 100%; border-collapse: collapse; min-width: 820px; }
.rule-table th { text-align: left; font-size: 13px; color: #64748b; font-weight: 500; padding: 6px 10px 6px 0; }
.rule-table td { padding: 8px 10px 8px 0; vertical-align: top; border-top: 1px solid #eef1f6; }
.rule-table small { display: block; font-size: 11px; }
.input.mini, .select.mini { width: 96px; }
.input.wide { width: 100%; min-width: 160px; }
.rule-hint { margin-top: 14px; font-size: 12px; }
</style>
