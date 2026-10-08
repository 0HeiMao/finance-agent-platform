<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import { departmentsText, parseDepartments } from '../utils/format'

const sidebarOpen = ref(false)
const departments = ref([])
const unrestrictedUsers = ref([])
const summary = ref(null)
const me = ref(null)
const knownDepartments = ref([])
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busyKey = ref('')
const drafts = reactive({})

const RULES = [
  {
    title: '不受限账号（部门留空）',
    tone: 'ok',
    items: [
      '看板、收入/成本分析、预警、Word/PPT 报告、Agent 都是全公司口径（读 department 为空的那行指标）。',
      '知识库检索返回所有片段；管理入口全部开放。'
    ]
  },
  {
    title: '限定部门账号',
    tone: 'warn',
    items: [
      '只统计该部门的数据行：指标与预警读该部门的 department 口径行，明细钻取只返回本部门交易。',
      '报告与 PPT 在标题/封面写明「数据范围：部门 X」，Agent 证据里也会带同一句，避免数字被当公司口径引用。',
      '知识库只返回该部门与未标注部门的片段；管理接口一律 403。',
      '部门为空的数据行（未标注部门）归全公司口径，不属于任何部门账号。'
    ]
  },
  {
    title: '多部门账号（一个账号绑定多个部门）',
    tone: 'warn',
    items: [
      '看到的是这些部门的并集：金额相加、比率按相加后的金额重算（比率不能相加）。',
      '部门筛选、部门权限页都会把它算进每一个绑定的部门；同名部门只写一次。',
      '越界的口径切换被拒绝：只能在自己绑定的部门里收窄，切不到没绑定的部门。'
    ]
  }
]

const scopeCount = computed(() => departments.value.filter((item) => item.user_count).length)

/**
 * 多部门账号在 `departments[].users` 里会出现多次（每个绑定的部门一次），
 * 直接展开渲染会得到重复 `:key` 的表格行（Vue 会告警，且同一账号出现两行）。
 * 这里按 id 去重、保持「不受限账号在前」的顺序。
 */
const editableUsers = computed(() => {
  const seen = new Map()
  for (const user of [...unrestrictedUsers.value, ...departments.value.flatMap((item) => item.users)]) {
    if (!seen.has(user.id)) seen.set(user.id, user)
  }
  return [...seen.values()]
})

function short(value) {
  const raw = Number(String(value ?? 0).replace(/,/g, ''))
  if (!Number.isFinite(raw) || raw === 0) return '0.00'
  if (Math.abs(raw) >= 1e8)
    return `${(raw / 1e8).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 亿元`
  if (Math.abs(raw) >= 1e4)
    return `${(raw / 1e4).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 万元`
  return `${raw.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`
}

function exact(value) {
  return Number(String(value ?? 0).replace(/,/g, '')).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/departments')
    const payload = await response.json()
    departments.value = payload.departments || []
    unrestrictedUsers.value = payload.unrestricted_users || []
    summary.value = payload.summary || null
    me.value = payload.me || null
    knownDepartments.value = departments.value.map((item) => item.name)
    ;[...unrestrictedUsers.value, ...departments.value.flatMap((item) => item.users)].forEach((user) => {
      drafts[user.id] = departmentsText(user.departments || user.department)
    })
  } catch (e) {
    error.value = `读取部门权限失败：${e.message}`
    departments.value = []
    unrestrictedUsers.value = []
  } finally {
    loading.value = false
  }
}

async function saveUser(user) {
  busyKey.value = `user-${user.id}`
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/admin/users/${user.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ departments: parseDepartments(drafts[user.id]) })
    })
    const updated = await response.json()
    notice.value = updated.unrestricted
      ? `已保存：${updated.username} 现在不受部门限制。`
      : `已保存：${updated.username} 限定为「${departmentsText(updated.departments)}」。`
    await load()
  } catch (e) {
    error.value = `保存失败：${e.message}`
  } finally {
    busyKey.value = ''
  }
}

function useDepartment(name) {
  const target = unrestrictedUsers.value[0]
  if (target) drafts[target.id] = name
  notice.value = `已把「部门 ${name}」填到 ${target ? target.username : '第一个不受限账号'} 的输入框，确认后点保存。`
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
            <h1>部门权限设置</h1>
            <p>部门决定财务数据与知识库的可见范围：留空＝不受限制（管理员），填写后只统计本部门口径。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <p v-if="loading && !departments.length" class="muted">正在读取部门权限…</p>

      <section v-else-if="error && !departments.length" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else>
        <section v-if="error" class="state-box error-state">
          <strong>操作未完成</strong>
          <p>{{ error }}</p>
        </section>
        <section v-else-if="notice" class="state-box notice">{{ notice }}</section>

        <section class="page-card">
          <div class="card-head">
            <h2>权限规则</h2>
            <span>部门的判定只认服务端记录，请求头自述一律忽略（避免伪造提权）</span>
          </div>
          <div class="rule-grid">
            <div v-for="rule in RULES" :key="rule.title" class="rule-card" :class="rule.tone">
              <strong>{{ rule.title }}</strong>
              <ul>
                <li v-for="(line, index) in rule.items" :key="index">{{ line }}</li>
              </ul>
            </div>
          </div>
        </section>

        <section v-if="summary" class="page-card">
          <div class="card-head">
            <h2>总览</h2>
            <span>部门不是可新建的表，而是从数据行里长出来的：有明细、有指标、或被账号引用就会出现</span>
          </div>
          <div class="settings-grid">
            <div>
              <span>数据中出现过的部门</span><strong>{{ summary.departments_with_data }}</strong>
            </div>
            <div>
              <span>已绑定账号的部门</span><strong>{{ summary.departments_with_users }}</strong>
            </div>
            <div>
              <span>不受限账号</span><strong>{{ summary.unrestricted_users }}</strong>
            </div>
            <div>
              <span>账号总数</span><strong>{{ summary.total_users }}</strong>
            </div>
          </div>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>账号 → 部门</h2>
            <span>修改自己的部门请到 System 设置（管理接口拒绝自改，避免把自己锁在管理入口之外）</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>当前范围</th>
                  <th>部门（可编辑，多个用「、」分隔）</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="user in editableUsers" :key="user.id">
                  <td>
                    {{ user.username }}
                    <span v-if="user.is_self" class="level medium">当前账号</span>
                  </td>
                  <td>
                    <span class="level" :class="user.unrestricted ? 'low' : 'medium'">
                      {{
                        user.unrestricted
                          ? '全公司'
                          : `部门 ${departmentsText(user.departments) || user.department}`
                      }}
                    </span>
                  </td>
                  <td>
                    <input
                      v-model.trim="drafts[user.id]"
                      class="select"
                      list="department-options"
                      maxlength="500"
                      placeholder="留空＝不受限；多个用「、」分隔"
                      :disabled="user.is_self"
                    />
                  </td>
                  <td>
                    <button
                      class="btn"
                      :disabled="user.is_self || busyKey === `user-${user.id}`"
                      @click="saveUser(user)"
                    >
                      保存
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <datalist id="department-options">
            <option v-for="name in knownDepartments" :key="name" :value="name" />
          </datalist>
          <p class="muted">
            切到受限部门后，该账号的看板、分析、报告、Agent
            只统计本部门明细；知识库只返回本部门与未标注部门的片段。
          </p>
        </section>

        <section class="page-card">
          <div class="card-head">
            <h2>部门列表（共 {{ departments.length }} 个，{{ scopeCount }} 个已绑定账号）</h2>
            <span>按「有账号优先、费用金额倒序」排列</span>
          </div>
          <div v-if="!departments.length" class="state-box empty-state">
            <strong>还没有任何部门数据</strong>
            <p>导入财务明细后，这里的部门会跟着出现。</p>
          </div>
          <ul v-else class="dir-list">
            <li v-for="item in departments" :key="item.name">
              <div class="dir-head">
                <strong>{{ item.label }}</strong>
                <span v-if="item.user_count" class="level medium">{{ item.user_count }} 个账号</span>
                <span v-else class="level low">未绑定账号</span>
              </div>
              <div class="dept-meta">
                <code>{{ item.transactions }} 行明细</code>
                <code>指标 {{ item.metric_rows }} 行 / 预警 {{ item.alert_rows }} 条</code>
                <code
                  >期间
                  {{
                    item.periods.length
                      ? `${item.periods[0]} ~ ${item.periods[item.periods.length - 1]}`
                      : '—'
                  }}</code
                >
              </div>
              <small>
                收入 {{ short(item.revenue_amount) }}（{{ exact(item.revenue_amount) }} 元） · 费用
                {{ short(item.expense_amount) }}（{{ exact(item.expense_amount) }} 元）
              </small>
              <div v-if="item.users.length" class="row-actions">
                <span v-for="user in item.users" :key="user.id" class="chip">{{ user.username }}</span>
              </div>
              <div v-else class="row-actions">
                <button class="btn" @click="useDepartment(item.name)">把此部门填给待分配账号</button>
              </div>
            </li>
          </ul>
        </section>
      </template>
    </main>
  </div>
</template>
