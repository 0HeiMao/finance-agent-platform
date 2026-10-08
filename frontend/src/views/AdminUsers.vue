<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'
import { departmentsText, parseDepartments } from '../utils/format'

const PAGE_SIZE = 50

const sidebarOpen = ref(false)
const users = ref([])
const total = ref(0)
const offset = ref(0)
const departments = ref([])
const unrestrictedCount = ref(0)
const query = ref('')
const departmentFilter = ref('')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busyId = ref(null)
const confirmId = ref(null)
const drafts = reactive({})

const summary = computed(() => {
  const filtered = query.value || departmentFilter.value
  return filtered
    ? `命中 ${total.value} 个账号（筛选后）`
    : `共 ${total.value} 个账号`
})

async function load() {
  loading.value = true
  error.value = ''
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
  if (query.value) params.set('query', query.value)
  if (departmentFilter.value === '__unrestricted__') params.set('unrestricted', 'true')
  else if (departmentFilter.value) params.set('department', departmentFilter.value)
  try {
    const response = await apiFetch(`/admin/users?${params.toString()}`)
    const payload = await response.json()
    users.value = payload.data
    total.value = payload.total || 0
    departments.value = payload.departments || []
    unrestrictedCount.value = payload.unrestricted_count || 0
    users.value.forEach((user) => { drafts[user.id] = departmentsText(user.departments || user.department) })
  } catch (e) {
    error.value = `读取用户列表失败：${e.message}`
    users.value = []
    total.value = 0
  } finally {
    loading.value = false
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

// 输入框边打边查会每敲一个字打一次接口；停 300ms 再查
let timer = null
watch(query, () => {
  clearTimeout(timer)
  timer = setTimeout(reload, 300)
})
watch(departmentFilter, reload)

async function saveDepartment(user) {
  busyId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    const wanted = parseDepartments(drafts[user.id])
    const response = await apiFetch(`/admin/users/${user.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ departments: wanted }),
    })
    const updated = await response.json()
    user.department = updated.department
    user.departments = updated.departments
    user.unrestricted = updated.unrestricted
    drafts[user.id] = departmentsText(updated.departments)
    notice.value = updated.unrestricted
      ? `已保存：${updated.username} 部门为空，现在是管理员（不受部门限制）。`
      : `已保存：${updated.username} 只能检索「${departmentsText(updated.departments)}」与未标注部门的片段。`
  } catch (e) {
    error.value = `保存失败：${e.message}`
  } finally {
    busyId.value = null
  }
}

async function removeUser(user) {
  busyId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    await apiFetch(`/admin/users/${user.id}`, { method: 'DELETE' })
    notice.value = `已删除账号 ${user.username}。财务数据是按批次入库的，不受影响。`
    confirmId.value = null
    // 删掉的是本页最后一条时往后挪一页，否则会停在一个空页上显示「还没有用户」
    if (users.value.length === 1 && offset.value >= PAGE_SIZE) {
      offset.value -= PAGE_SIZE
    }
    await load()
  } catch (e) {
    error.value = `删除失败：${e.message}`
  } finally {
    busyId.value = null
  }
}

async function forceLogout(user) {
  busyId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/admin/users/${user.id}/logout`, { method: 'POST' })
    const updated = await response.json()
    user.token_version = updated.token_version
    notice.value = `已强制下线 ${updated.username}：它此前签发的令牌全部失效（会话版本 ${updated.token_version}）。`
      + '对方正在打开的页面下一次请求就会被踢回登录页，重新登录即可继续使用。'
  } catch (e) {
    error.value = `强制下线失败：${e.message}`
  } finally {
    busyId.value = null
  }
}

async function unlock(user) {
  busyId.value = user.id
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/admin/users/${user.id}/unlock`, { method: 'POST' })
    const updated = await response.json()
    user.failed_attempts = updated.failed_attempts
    user.locked = updated.locked
    user.locked_until = updated.locked_until
    notice.value = `已解锁 ${updated.username}：清掉 ${updated.cleared_attempts} 次连续失败记录，现在可以立刻重新登录。`
      + '（解锁不等于重置口令，也不会让已登录的会话掉线）'
  } catch (e) {
    error.value = `解锁失败：${e.message}`
  } finally {
    busyId.value = null
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
            <h1>用户管理</h1>
            <p>部门决定知识库检索与财务数据的可见范围：留空＝不受限制（管理员）；可填多个部门（用「、」分隔），看到的是这些部门的并集。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '读取中…' : '↻ 重新读取' }}</button>
        </div>
      </header>

      <p v-if="loading && !users.length" class="muted">正在读取用户列表…</p>

      <section v-else-if="error && !users.length" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else>
        <section v-if="error" class="state-box error-state"><strong>操作未完成</strong><p>{{ error }}</p></section>
        <section v-else-if="notice" class="state-box notice">{{ notice }}</section>

        <section v-if="!users.length" class="state-box empty-state">
          <strong>{{ query || departmentFilter ? '没有符合条件的账号' : '还没有用户' }}</strong>
          <p v-if="query || departmentFilter">换个用户名或部门再查；也可以把筛选清空看全部账号。</p>
          <p v-else>账号通过 /api/v1/auth/register 注册，注册后会出现在这里。</p>
        </section>

        <section v-else class="page-card">
          <div class="card-head">
            <h2>{{ summary }}</h2>
            <span>修改自己的部门请到 System 设置（管理接口会拒绝自改，避免把自己锁在管理入口之外）</span>
          </div>

          <div class="filter-row">
            <label class="field">
              <span>用户名</span>
              <input v-model.trim="query" class="select" maxlength="100" placeholder="按用户名模糊搜索">
            </label>
            <label class="field">
              <span>部门</span>
              <select v-model="departmentFilter" class="select">
                <option value="">全部部门</option>
                <option value="__unrestricted__">不受限（管理员）· {{ unrestrictedCount }}</option>
                <option v-for="name in departments" :key="name" :value="name">{{ name }}</option>
              </select>
            </label>
            <span class="muted">每页 {{ PAGE_SIZE }} 条，翻页不丢筛选条件</span>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>id</th><th>用户名</th><th>角色</th><th>部门（可编辑，多个用「、」分隔）</th><th>登录</th><th>会话</th><th>创建时间</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="user in users" :key="user.id">
                  <td>{{ user.id }}</td>
                  <td>
                    {{ user.username }}
                    <span v-if="user.is_self" class="level medium">当前账号</span>
                  </td>
                  <td>
                    <span class="level" :class="user.unrestricted ? 'low' : 'medium'">
                      {{ user.unrestricted ? '管理员' : (user.departments || []).length > 1 ? `限定 ${user.departments.length} 个部门` : '限定部门' }}
                    </span>
                  </td>
                  <td>
                    <input
                      v-model.trim="drafts[user.id]"
                      class="select"
                      maxlength="500"
                      placeholder="留空＝不受限；多个用「、」分隔"
                      :disabled="user.is_self"
                    >
                  </td>
                  <td>
                    <span v-if="user.locked" class="level high" :title="user.locked_until || ''">
                      已锁定
                    </span>
                    <span v-else-if="user.failed_attempts" class="level medium">
                      失败 {{ user.failed_attempts }} 次
                    </span>
                    <span v-else class="muted">正常</span>
                  </td>
                  <td>
                    <span v-if="!user.token_version" class="muted">未作废过</span>
                    <span v-else>已作废 {{ user.token_version }} 次</span>
                  </td>
                  <td>{{ user.created_at || '—' }}</td>
                  <td>
                    <div class="row-actions">
                      <button
                        class="btn"
                        :disabled="user.is_self || busyId === user.id"
                        @click="saveDepartment(user)"
                      >保存部门</button>
                      <button
                        class="btn"
                        :disabled="!user.locked || busyId === user.id"
                        title="提前解除登录失败锁定：清掉失败次数与锁定时间（不重置口令，也不会让已登录的会话掉线）"
                        @click="unlock(user)"
                      >解锁</button>
                      <button
                        class="btn"
                        :disabled="user.is_self || busyId === user.id"
                        title="作废该账号所有已签发的令牌：对方下次请求就会掉线，重新登录即可继续用"
                        @click="forceLogout(user)"
                      >强制下线</button>
                      <template v-if="confirmId !== user.id">
                        <button class="btn danger" :disabled="user.is_self" @click="confirmId = user.id">删除</button>
                      </template>
                      <template v-else>
                        <button class="btn danger" :disabled="busyId === user.id" @click="removeUser(user)">确认删除</button>
                        <button class="btn" @click="confirmId = null">取消</button>
                      </template>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <PagePager :total="total" :limit="PAGE_SIZE" :offset="offset" :loading="loading" unit="个账号" @change="turn" />

          <p class="muted">
            删除账号只移除登录凭据；财务数据按导入批次存在，需要清理请到「导入批次」撤销对应批次。
            令牌是无状态的（签出去就收不回来），所以「强制下线」的做法是把该账号的<strong>会话版本 +1</strong>：
            它此前签发的每一份令牌立刻作废，对方下次请求就掉线，重新登录即可继续用——数据不会被改动。
            登录连续失败会按次数指数退避地锁定账号（默认第 5 次起 60 秒、之后翻倍、最多 900 秒），
            锁着的时候<strong>口令正确也进不来</strong>：「登录」列显示「已锁定」，点「解锁」可以立刻放行。
          </p>
        </section>
      </template>
    </main>
  </div>
</template>
