<script setup>
import { nextTick, ref } from 'vue'
import { readSSE } from '../api/client'
import { withScope } from '../api/scope'
import { renderMarkdown } from '../utils/markdown'

/**
 * 财务分析 Agent 对话框。
 * - 默认（卡片形态）：嵌在看板底部，只给输入框和消息流。
 * - `full`（工作台形态）：独立页面用，消息区更高、带示例问题和清空会话。
 * 两种形态共用同一套流式逻辑，避免「看板里的助手」和「专属页面的助手」行为漂移。
 */
const props = defineProps({
  full: { type: Boolean, default: false },
  examples: { type: Array, default: () => [] },
})

const input = ref('')
const messages = ref([])
const loading = ref(false)
const error = ref('')
const listRef = ref(null)
let controller = null

const KIND_LABEL = {
  financial_metric: '财务汇总表',
  financial_alert: '财务预警',
  financial_transaction: '明细账',
  knowledge_base: '知识库',
}

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
  error.value = ''
  messages.value.push({ role: 'user', text: question })
  input.value = ''
  const answer = { role: 'assistant', text: '', sources: [], loading: true }
  messages.value.push(answer)
  loading.value = true
  controller = new AbortController()
  await scrollToBottom()
  try {
    // 财务范围只认服务端记录 + 当前选择的口径（请求头自述一律不算，见后端注释）
    await readSSE(withScope('/ai/agent/chat/stream'), { question }, {
      signal: controller.signal,
      onDelta: (chunk) => {
        answer.text += chunk
        scrollToBottom()
      },
      onDone: (payload) => {
        answer.sources = payload?.sources || []
        answer.loading = false
      },
      onError: (e) => {
        answer.loading = false
        error.value = e.message || 'Agent 暂时无法回答'
      },
    })
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

function stop() {
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
          >{{ example }}</button>
        </div>
      </div>

      <div
        v-for="(message, index) in messages"
        :key="index"
        class="agent-message"
        :class="message.role"
      >
        <span class="agent-avatar">{{ message.role === 'user' ? '我' : 'AI' }}</span>
        <div class="agent-bubble">
          <!-- 模型回答按 Markdown 渲染（内部先转义再解析），用户提问保持纯文本 -->
          <template v-if="message.text && message.role === 'assistant'">
            <div class="md-body" v-html="renderMarkdown(message.text)"></div>
          </template>
          <template v-else-if="message.text">{{ message.text }}</template>
          <span v-else-if="message.loading" class="muted">正在检索财务数据与知识库…</span>
          <span v-if="message.loading">▋</span>
          <div v-if="message.sources && message.sources.length" class="sources">
            依据：{{ sourceText(message.sources) }}
          </div>
        </div>
      </div>
    </div>

    <div v-if="error" class="agent-error">
      {{ error }}
      <button class="btn" @click="sendMessage">重试</button>
    </div>

    <div class="agent-input-row">
      <input
        v-model="input"
        :disabled="loading"
        placeholder="输入财务分析问题，例如「2026-07 的费用异常在哪里？」"
        @keyup.enter="sendMessage"
      >
      <button class="btn primary" @click="loading ? stop() : sendMessage()">
        {{ loading ? '停止' : '发送' }}
      </button>
    </div>
  </section>
</template>
