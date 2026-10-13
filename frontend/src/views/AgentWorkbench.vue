<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppSidebar from '../components/AppSidebar.vue'
import AgentChat from '../components/AgentChat.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import {
  listAgentSessions,
  getAgentUsage,
  createAgentSession,
  getAgentSession,
  deleteAgentSession,
  getAlertContext
} from '../api/agent'
import { currentUser, loadCurrentUser } from '../api/session'
import { departmentsText } from '../utils/format'

const sidebarOpen = ref(false)
const historyOpen = ref(false)
const historyCollapsed = ref(false)
const busy = ref(false)
const listLoading = ref(true)
const lifetime = new AbortController()
onBeforeUnmount(() => lifetime.abort())
const user = currentUser()
onMounted(loadCurrentUser)

// Agent 的证据范围由后端按账号部门收窄（scope_of(user)），这里只把同一件事显示出来：
// 受限账号如果不提示，很容易把「部门 097 的费用」当成公司费用引用出去。
const scope = computed(() => {
  const me = user.value
  if (!me || me.unrestricted) return null
  const names = departmentsText(me.departments) || me.department || ''
  const multi = names.includes('、')
  return {
    department: names,
    label: multi ? `部门 ${names}（${names.split('、').length} 个部门的并集）` : `部门 ${names}`,
    unrestricted: false,
    note: multi
      ? '当前账号统计的是这几个部门的并集（金额相加、比率按合计重算），回答里的金额不是全公司口径。'
      : '当前账号只统计本部门明细，回答里的金额不是全公司口径。'
  }
})

// 示例只表达查询意图，可用期间和实际证据由后端检查。
const EXAMPLES = [
  '有哪些可用的财务期间？',
  '本月收支差额是多少？',
  '哪些部门支出最高？',
  '成本率异常的预警阈值是多少？'
]

/**
 * 会话历史与用量（⑮）。
 *
 * 会话列表只有「打开某个会话」这一个用途：点开就把落库的消息灌回对话框，问答本身仍然
 * 在 AgentChat 里跑。`chatKey` 用来强制重建对话框——历史消息是初始化时读进去的，
 * 换会话时靠重建比在组件里做双向同步可靠得多（也顺手把没跑完的流清掉）。
 */
const sessions = ref([])
const sessionId = ref(null)
const history = ref([])
const chatKey = ref(0)
const usage = ref(null)
const listError = ref('')
const route = useRoute()
const router = useRouter()
const alertContext = ref(null)
const contextLoading = ref(false)
const contextError = ref('')
let contextRequest = null
onBeforeUnmount(() => contextRequest?.abort())
async function loadContext() {
  contextRequest?.abort()
  const request = new AbortController()
  contextRequest = request
  alertContext.value = null
  contextError.value = ''
  contextLoading.value = false
  resetChat(null, [])
  const id = route.query.alert
  if (!id) return
  if (typeof id !== 'string' || !/^[1-9]\d*$/.test(id)) {
    contextError.value = '预警编号无效，请从预警列表重新进入。'
    return
  }
  contextLoading.value = true
  try {
    const payload = await getAlertContext(id, request.signal)
    if (!request.signal.aborted) alertContext.value = payload
  } catch (error) {
    if (!request.signal.aborted) contextError.value = error.message || '无法读取预警'
  } finally {
    if (!request.signal.aborted) contextLoading.value = false
  }
}
watch(() => route.query.alert, loadContext, { immediate: true })

async function exitAlert() {
  if (route.query.alert) await router.replace('/agent')
  await loadContext()
}

async function loadSessions() {
  try {
    const payload = await listAgentSessions(lifetime.signal)
    sessions.value = payload.data || []
    listError.value = ''
  } catch (e) {
    if (lifetime.signal.aborted) return
    listError.value = e.message || '会话列表读取失败'
  } finally {
    listLoading.value = false
  }
}

async function loadUsage() {
  try {
    usage.value = await getAgentUsage(lifetime.signal)
  } catch {
    // 用量只是附带信息：取不到不该挡住问答
    usage.value = null
  }
}

function resetChat(nextSessionId, messages) {
  sessionId.value = nextSessionId
  history.value = messages || []
  chatKey.value += 1
}

async function newSession() {
  if (busy.value) return
  busy.value = true
  listError.value = ''
  try {
    const created = await createAgentSession(lifetime.signal)
    if (route.query.alert) await router.replace('/agent')
    alertContext.value = null
    resetChat(created.id, [])
    historyOpen.value = false
    await loadSessions()
  } catch (e) {
    if (lifetime.signal.aborted) return
    listError.value = e.message || '新建会话失败'
  } finally {
    busy.value = false
  }
}

async function openSession(id) {
  if (busy.value) return
  busy.value = true
  listError.value = ''
  try {
    const detail = await getAgentSession(id, lifetime.signal)
    const savedContext = [...(detail.messages || [])]
      .reverse()
      .flatMap((message) => message.execution_trace || [])
      .find((step) => step.kind === 'alert_context')
    const verifiedContext = savedContext ? await getAlertContext(savedContext.id, lifetime.signal) : null
    if (route.query.alert) await router.replace('/agent')
    alertContext.value = verifiedContext
    resetChat(id, detail.messages || [])
    historyOpen.value = false
  } catch (e) {
    if (lifetime.signal.aborted) return
    listError.value = e.message || '打开会话失败'
  } finally {
    busy.value = false
  }
}

async function removeSession(id) {
  if (busy.value) return
  busy.value = true
  listError.value = ''
  try {
    await deleteAgentSession(id, lifetime.signal)
    if (sessionId.value === id) resetChat(null, [])
    await Promise.all([loadSessions(), loadUsage()])
  } catch (e) {
    if (lifetime.signal.aborted) return
    listError.value = e.message || '删除会话失败'
  } finally {
    busy.value = false
  }
}

function onSaved() {
  // 一问一答结束：刷新列表（标题、问答数、token 都变了）与用量
  loadSessions()
  loadUsage()
}

onMounted(() => {
  loadSessions()
  loadUsage()
})
</script>

<template>
  <div class="app agent-page">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>财务分析 Agent</h1>
            <p>把问题交给 Agent，将结论追溯到数据与制度。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher v-if="!route.query.alert && !alertContext" />
        </div>
      </header>

      <DataScopeNotice v-if="!route.query.alert && !alertContext" :scope="scope" />
      <section v-if="contextLoading" class="state-box" role="status">正在核验预警权限与分析范围…</section>
      <section v-else-if="contextError" class="state-box error-state" role="alert">
        {{ contextError }} <button class="btn" @click="loadContext">重试</button
        ><router-link class="btn" to="/agent">返回普通问答</router-link>
      </section>
      <section v-else-if="alertContext" class="alert-context card">
        <div>
          <span class="eyebrow">预警分析 · #{{ alertContext.id }}</span>
          <h2>{{ alertContext.type }}</h2>
          <p>{{ alertContext.period }} · {{ alertContext.scope_label }} · {{ alertContext.description }}</p>
          <small>分析范围已按预警锁定。问题已填入输入框，确认后发送。</small>
        </div>
        <button class="btn" @click="exitAlert">退出预警分析</button>
      </section>

      <div class="workbench-toolbar">
        <span class="eyebrow">✦ AGENT WORKSPACE</span
        ><button class="btn history-toggle" :aria-expanded="historyOpen" @click="historyOpen = !historyOpen">
          会话记录</button
        ><button
          class="btn desktop-history-toggle"
          :aria-expanded="!historyCollapsed"
          @click="historyCollapsed = !historyCollapsed"
        >
          {{ historyCollapsed ? '展开会话' : '收起会话' }}
        </button>
      </div>
      <button
        v-if="historyOpen"
        class="history-scrim"
        aria-label="关闭会话记录"
        @click="historyOpen = false"
      />
      <div class="agent-layout" :class="{ 'history-collapsed': historyCollapsed }">
        <section
          class="card session-card"
          :class="{ 'history-open': historyOpen }"
          @keydown.esc="historyOpen = false"
        >
          <div class="card-head">
            <h2>会话历史</h2>
            <button class="btn primary" :disabled="busy" @click="newSession">新建会话</button>
            <button class="btn history-toggle" @click="historyOpen = false">关闭</button>
          </div>
          <div v-if="listError" class="state-box error-state" role="alert">
            {{ listError }} <button class="btn" :disabled="busy" @click="loadSessions">刷新列表</button>
          </div>
          <p v-if="listLoading" class="muted" role="status">正在读取会话…</p>
          <ul v-else-if="sessions.length" class="session-list">
            <li
              v-for="item in sessions"
              :key="item.id"
              class="session-row"
              :class="{ active: item.id === sessionId }"
            >
              <button class="session-open" :disabled="busy" @click="openSession(item.id)">
                <span class="session-title">{{ item.display_title }}</span>
                <span class="muted session-meta">
                  {{ item.answers }} 问 · {{ item.total_tokens }} tokens
                </span>
              </button>
              <button
                class="btn danger session-delete"
                :disabled="busy"
                :aria-label="`删除会话：${item.display_title}`"
                @click="removeSession(item.id)"
              >
                删除
              </button>
            </li>
          </ul>
          <p v-else class="muted">还没有会话。问一句就会自动建一个，也可以点「新建会话」先占位再提问。</p>

          <details v-if="usage" class="usage-panel">
            <summary>近 7 天用量</summary>
            <p>{{ usage.data.answers }} 次回答 · {{ usage.data.total_tokens }} tokens</p>
            <p class="muted">
              其中 {{ usage.data.api_answers }} 次由接口上报真实用量，{{ usage.data.estimated_answers }}
              次按字数估算
            </p>
            <!-- 按天明细默认收起来：它带日期，展开状态会每天变一次，截图基线会被它拖成「天天都红」 -->
            <details class="usage-days-details">
              <summary>按天明细（近 7 天）</summary>
              <ul class="usage-days">
                <li v-for="day in usage.series" :key="day.date">
                  <span>{{ day.date.slice(5) }}</span>
                  <span class="muted">{{ day.answers }} 问 · {{ day.total_tokens }} tokens</span>
                </li>
              </ul>
            </details>
          </details>
        </section>

        <!-- key 换会话就重建：历史消息只在初始化时读一次 -->
        <AgentChat
          v-if="!contextLoading && !contextError"
          :key="chatKey"
          full
          :examples="EXAMPLES"
          :session-id="sessionId"
          :history="history"
          :initial-question="alertContext?.question || ''"
          :alert-id="alertContext?.id || null"
          @update:session-id="sessionId = $event"
          @saved="onSaved"
        />
      </div>

      <div class="footer">智能财务分析平台 · 回答可能不完整，请对照「依据」列出的来源人工复核</div>
    </main>
  </div>
</template>

<style scoped>
.agent-layout {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.session-card {
  padding: 16px;
}

.session-list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: grid;
  gap: 8px;
}

.session-row {
  display: flex;
  align-items: stretch;
  gap: 6px;
  border-radius: 8px;
}

.session-row.active {
  outline: 1px solid var(--accent, #3b82f6);
}

.session-open {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 2px;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 6px 8px;
  cursor: pointer;
  color: inherit;
}

.session-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  font-size: 12px;
}

.session-delete {
  align-self: center;
}

.usage-panel {
  margin-top: 16px;
  border-top: 1px solid var(--border, #e5e7eb);
  padding-top: 12px;
}

.usage-days-details {
  margin-top: 8px;
  font-size: 12px;
}

.usage-days-details summary {
  cursor: pointer;
  color: var(--muted, #6b7280);
}

.usage-days {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: grid;
  gap: 2px;
  font-size: 12px;
}

.usage-days li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

@media (max-width: 900px) {
  .agent-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
