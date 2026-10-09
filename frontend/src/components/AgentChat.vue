<script setup>
import { nextTick, ref } from 'vue'
import { apiFetch, readSSE } from '../api/client'
import { withScope } from '../api/scope'
import { renderMarkdown } from '../utils/markdown'

/**
 * 财务分析 Agent 对话框。
 * - 默认（卡片形态）：嵌在看板底部，只给输入框和消息流。
 * - `full`（工作台形态）：独立页面用，消息区更高、带示例问题和清空会话。
 * 两种形态共用同一套流式逻辑，避免「看板里的助手」和「专属页面的助手」行为漂移。
 *
 * ⑮ 起对话有了身份：`sessionId` 由上层传入（工作台的会话历史），流式过程中服务端会
 * 在 `event: start` 里回一个新的 id，这里再抛回上层——所以「停止」与「会话列表」都有
 * 对象可用，而不是只在浏览器内存里存在一段聊天记录。
 */
const props = defineProps({
  full: { type: Boolean, default: false },
  examples: { type: Array, default: () => [] },
  sessionId: { type: [Number, String], default: null },
  history: { type: Array, default: () => [] }
})

const emit = defineEmits(['update:sessionId', 'saved'])

const input = ref('')
const loading = ref(false)
const error = ref('')
const listRef = ref(null)
// 「重试」要重发的是上一条问题：发完之后输入框已经被清空，读 input.value 只会拿到空串
const lastQuestion = ref('')
const session = ref(props.sessionId)
let controller = null

const KIND_LABEL = {
  financial_metric: '财务汇总表',
  financial_alert: '财务预警',
  financial_transaction: '明细账',
  knowledge_base: '知识库'
}

/** 落库的会话记录 → 界面上的消息（名字对齐后端，界面不直接吃接口形状）。 */
function fromHistory(item) {
  return {
    role: item.role,
    text: item.content || '',
    sources: item.sources || [],
    tokens: item.total_tokens ? { total_tokens: item.total_tokens, usage_source: item.usage_source } : null,
    stopped: item.status === 'stopped',
    error: item.status === 'error' ? item.error || '' : ''
  }
}

const messages = ref((props.history || []).map(fromHistory))

function sourceLabel(source) {
  const item = typeof source === 'string' ? { name: source } : source
  const name = item.name || ''
  const label = KIND_LABEL[item.kind]
  // 后端给财务证据起的名字本身就以类型开头（如「财务汇总表 2026-07」），再加前缀会变成
  // 「财务汇总表·财务汇总表 2026-07」；知识库片段只给文件名，才需要补「知识库·」。
  if (!label || name.startsWith(label)) return name
  return `${label}·${name}`
}

/** 同一份材料常有多个片段，展示时按标签去重，避免「依据」里同一个文件名刷三遍。 */
function sourceText(sources) {
  const labels = (sources || []).map(sourceLabel).filter(Boolean)
  return [...new Set(labels)].join('、')
}

async function scrollToBottom() {
  await nextTick()
  if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
}

async function ask(text) {
  const question = (text || '').trim()
  if (!question || loading.value) return
  lastQuestion.value = question
  error.value = ''
  messages.value.push({ role: 'user', text: question })
  input.value = ''
  const answer = { role: 'assistant', text: '', sources: [], loading: true, tokens: null }
  messages.value.push(answer)
  loading.value = true
  controller = new AbortController()
  await scrollToBottom()
  // 已有会话就接上去，否则让服务端在流式开始时建一个（date: start 事件带回它的 id）
  const path = session.value ? `/ai/agent/chat/stream?session_id=${session.value}` : '/ai/agent/chat/stream'
  try {
    // 财务范围只认服务端记录 + 当前选择的口径（请求头自述一律不算，见后端注释）
    await readSSE(
      withScope(path),
      { question },
      {
        signal: controller.signal,
        onStart: (payload) => {
          if (payload?.session_id && payload.session_id !== session.value) {
            session.value = payload.session_id
            emit('update:sessionId', payload.session_id)
          }
        },
        onDelta: (chunk) => {
          answer.text += chunk
          scrollToBottom()
        },
        onDone: (payload) => {
          answer.sources = payload?.sources || []
          answer.tokens = payload?.tokens || null
          // ⑰：这一轮到底带上了几轮历史——界面要说出来，否则用户以为模型「应该」记得
          answer.historyTurns = payload?.history_turns || 0
          answer.historyTruncated = payload?.history_truncated === true
          answer.stopped = payload?.status === 'stopped'
          answer.loading = false
          emit('saved')
        },
        onError: (e) => {
          answer.loading = false
          error.value = e.message || 'Agent 暂时无法回答'
        }
      }
    )
  } catch (e) {
    answer.loading = false
    if (e.name !== 'AbortError') error.value = e.message || '连接中断'
  } finally {
    loading.value = false
    controller = null
  }
}

function sendMessage() {
  ask(input.value)
}

/** 「重试」重发上一条问题：发过之后输入框已清空，再读 input.value 只会什么都不做。 */
function retry() {
  ask(lastQuestion.value || input.value)
}

/**
 * 停止生成：先让服务端把标志位置上，再断开本地连接。
 *
 * 只 abort 是不够的：服务端要到下一块才发现连接没了，那期间模型还在按 token 计费。
 * 置标志位之后生成器下一块就收尾，并把已经生成的部分按「已停止」落库。
 */
async function stop() {
  const id = session.value
  try {
    if (id) await apiFetch(`/ai/agent/sessions/${id}/stop`, { method: 'POST' })
  } catch (e) {
    error.value = e.message || '停止失败'
  }
  controller?.abort()
  loading.value = false
}

function clearSession() {
  messages.value = []
  error.value = ''
}
</script>

<template>
  <section class="card agent-card" :class="{ full: props.full }">
    <div class="card-head">
      <h2>财务分析 Agent</h2>
      <div class="agent-head-actions">
        <span v-if="props.full" class="muted">证据来自已导入的财务数据与知识库，模型只解释不编造数字</span>
        <span v-else>支持流式问答</span>
        <button v-if="props.full && messages.length" class="btn" :disabled="loading" @click="clearSession">
          清空会话
        </button>
        <router-link v-if="!props.full" class="btn" to="/agent">专属页面 →</router-link>
      </div>
    </div>

    <div ref="listRef" class="agent-messages">
      <div v-if="!messages.length" class="agent-empty">
        <p v-if="props.examples.length">试试这些问题：</p>
        <p v-else>可以问我：“本月费用异常在哪里？”</p>
        <div v-if="props.examples.length" class="agent-examples">
          <button
            v-for="example in props.examples"
            :key="example"
            class="btn"
            :disabled="loading"
            @click="ask(example)"
          >
            {{ example }}
          </button>
        </div>
      </div>

      <div v-for="(message, index) in messages" :key="index" class="agent-message" :class="message.role">
        <span class="agent-avatar">{{ message.role === 'user' ? '我' : 'AI' }}</span>
        <div class="agent-bubble">
          <!-- 模型回答按 Markdown 渲染（内部先转义再解析），用户提问保持纯文本 -->
          <template v-if="message.text && message.role === 'assistant'">
            <div class="md-body" v-html="renderMarkdown(message.text)"></div>
          </template>
          <template v-else-if="message.text">{{ message.text }}</template>
          <span v-else-if="message.loading" class="muted">正在检索财务数据与知识库…</span>
          <span v-if="message.loading">▋</span>
          <span v-if="message.stopped" class="muted stopped-note"
            >（这条回答被停止，只保留已生成的部分）</span
          >
          <p v-if="message.error" class="muted">{{ message.error }}</p>
          <div v-if="message.sources && message.sources.length" class="sources">
            依据：{{ sourceText(message.sources) }}
          </div>
          <div v-if="message.tokens" class="muted usage-note">
            本回答 {{ message.tokens.total_tokens }} tokens{{
              message.tokens.usage_source === 'estimated' ? '（按字数估算）' : ''
            }}
          </div>
          <!--
            ⑰：带上了几轮历史要说出来——「模型记不记得上一轮」是用户最关心的一件事，
            而它取决于预算（条数/字数）与上一轮有没有写完，界面上猜不出来。
          -->
          <div v-if="message.historyTurns" class="muted usage-note">
            已带上前 {{ message.historyTurns }} 轮对话{{ message.historyTruncated ? '（更早的已省略）' : '' }}
          </div>
        </div>
      </div>
    </div>

    <div v-if="error" class="agent-error">
      {{ error }}
      <button class="btn" @click="retry">重试</button>
    </div>

    <div class="agent-input-row">
      <input
        v-model="input"
        :disabled="loading"
        placeholder="输入财务分析问题，例如「2026-07 的费用异常在哪里？」"
        @keyup.enter="sendMessage"
      />
      <button class="btn primary" @click="loading ? stop() : sendMessage()">
        {{ loading ? '停止' : '发送' }}
      </button>
    </div>
  </section>
</template>
