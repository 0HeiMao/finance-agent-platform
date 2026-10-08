<script setup>
import { computed, onMounted, ref } from 'vue'
import AppSidebar from '../components/AppSidebar.vue'
import AgentChat from '../components/AgentChat.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import { apiFetch } from '../api/client'
import { currentUser, loadCurrentUser } from '../api/session'
import { departmentsText } from '../utils/format'

const sidebarOpen = ref(false)
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

// 示例问题按「库里确实有数据」来写：都能命中财务证据层或知识库，不会一问就答不上来
const EXAMPLES = [
  '2026-07 的费用异常在哪里？',
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

async function loadSessions() {
  try {
    const payload = await (await apiFetch('/ai/agent/sessions?limit=50')).json()
    sessions.value = payload.data || []
    listError.value = ''
  } catch (e) {
    listError.value = e.message || '会话列表读取失败'
  }
}

async function loadUsage() {
  try {
    usage.value = await (await apiFetch('/ai/agent/usage?days=7')).json()
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
  try {
    const created = await (
      await apiFetch('/ai/agent/sessions', {
        method: 'POST',
        body: JSON.stringify({})
      })
    ).json()
    resetChat(created.id, [])
    await loadSessions()
  } catch (e) {
    listError.value = e.message || '新建会话失败'
  }
}

async function openSession(id) {
  try {
    const detail = await (await apiFetch(`/ai/agent/sessions/${id}`)).json()
    resetChat(id, detail.messages || [])
  } catch (e) {
    listError.value = e.message || '打开会话失败'
  }
}

async function removeSession(id) {
  try {
    await apiFetch(`/ai/agent/sessions/${id}`, { method: 'DELETE' })
  } catch (e) {
    listError.value = e.message || '删除会话失败'
  }
  if (sessionId.value === id) resetChat(null, [])
  await Promise.all([loadSessions(), loadUsage()])
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
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>财务分析 Agent</h1>
            <p>基于已导入的财务数据与知识库作答：金额与比率由程序确定性汇总，模型只做解释与归因。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
        </div>
      </header>

      <DataScopeNotice :scope="scope" />

      <div class="agent-layout">
        <section class="card session-card">
          <div class="card-head">
            <h2>会话历史</h2>
            <button class="btn primary" @click="newSession">新建会话</button>
          </div>
          <p v-if="listError" class="state-box error-state">{{ listError }}</p>
          <ul v-if="sessions.length" class="session-list">
            <li
              v-for="item in sessions"
              :key="item.id"
              class="session-row"
              :class="{ active: item.id === sessionId }"
            >
              <button class="session-open" @click="openSession(item.id)">
                <span class="session-title">{{ item.display_title }}</span>
                <span class="muted session-meta">
                  {{ item.answers }} 问 · {{ item.total_tokens }} tokens
                </span>
              </button>
              <button class="btn danger session-delete" @click="removeSession(item.id)">删除</button>
            </li>
          </ul>
          <p v-else class="muted">还没有会话。问一句就会自动建一个，也可以点「新建会话」先占位再提问。</p>

          <div v-if="usage" class="usage-panel">
            <h3>近 7 天用量</h3>
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
          </div>
        </section>

        <!-- key 换会话就重建：历史消息只在初始化时读一次 -->
        <AgentChat
          :key="chatKey"
          full
          :examples="EXAMPLES"
          :session-id="sessionId"
          :history="history"
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
