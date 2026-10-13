<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { getAgentMessage, stopAgent, streamAgent } from '../api/agent'
import { renderMarkdown } from '../utils/markdown'
import AgentEvidence from './AgentEvidence.vue'

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
  history: { type: Array, default: () => [] },
  initialQuestion: { type: String, default: '' },
  alertId: { type: Number, default: null }
})

const emit = defineEmits(['update:sessionId', 'saved'])

const input = ref(props.initialQuestion)
const loading = ref(false)
const error = ref('')
const listRef = ref(null)
// 「重试」要重发的是上一条问题：发完之后输入框已经被清空，读 input.value 只会拿到空串
const lastQuestion = ref('')
const session = ref(props.sessionId)
let controller = null
const recovery = ref(null)
const recovering = ref(false)
const recoveryNotice = ref('')
const unknownRequest = ref(false)
let recoveryController = null
let recoveryTimer = null
let disposed = false
onBeforeUnmount(() => {
  disposed = true
  controller?.abort()
  recoveryController?.abort()
  clearTimeout(recoveryTimer)
})
const TOOL_LABELS = {
  list_periods: '检查可用期间',
  query_metrics: '查询财务指标',
  compare_expenses: '对比费用明细',
  search_policies: '检索相关制度'
}

const KIND_LABEL = {
  financial_metric: '财务汇总表',
  financial_alert: '财务预警',
  financial_transaction: '明细账',
  knowledge_base: '知识库'
}

/** 落库的会话记录 → 界面上的消息（名字对齐后端，界面不直接吃接口形状）。 */
function fromHistory(item) {
  const steps = new Map()
  for (const step of item.execution_trace || []) {
    if (step.kind === 'tool' || step.kind === 'tool_start') steps.set(step.step, step)
  }
  return {
    messageId: item.id,
    status: item.status,
    role: item.role,
    text: item.content || '',
    sources: item.sources || [],
    steps: [...steps.values()].map((step) =>
      step.status === 'running' && item.status !== 'streaming'
        ? { ...step, status: 'interrupted', summary: '该步骤没有完成记录，请核对后重试' }
        : step
    ),
    tokens: item.total_tokens ? { total_tokens: item.total_tokens, usage_source: item.usage_source } : null,
    stopped: item.status === 'stopped',
    interrupted: item.status === 'interrupted',
    error: item.status === 'error' ? item.error || '' : ''
  }
}

const messages = ref((props.history || []).map(fromHistory))

async function recoverAnswer(attempt = 0) {
  if (!recovery.value || recovering.value || disposed) return
  const pending = recovery.value
  clearTimeout(recoveryTimer)
  let stillRunning = false
  recovering.value = true
  recoveryController = new AbortController()
  recoveryNotice.value = '连接中断，正在查询原回答；不会重新调用模型。'
  try {
    const saved = await getAgentMessage(pending.sessionId, pending.messageId, recoveryController.signal)
    if (disposed) return
    if (saved.id !== pending.messageId || saved.role !== 'assistant')
      throw new Error('恢复记录不匹配，请查看会话历史')
    Object.assign(pending.answer, fromHistory(saved), { loading: false })
    if (saved.status === 'streaming') {
      stillRunning = true
      recoveryNotice.value = '原回答仍在生成。可继续检查结果或停止，暂不重复发送。'
    } else {
      recovery.value = null
      error.value = saved.status === 'error' ? saved.error || '服务执行失败，请核对后重试' : ''
      recoveryNotice.value =
        saved.status === 'ok'
          ? '已恢复保存的回答，没有重新调用模型。'
          : saved.status === 'stopped'
            ? '原回答已停止，已恢复保存内容。'
            : '原回答已中断，已恢复保存内容，可核对后重新提问。'
      emit('saved')
    }
  } catch (e) {
    if (e.name !== 'AbortError' && !disposed) {
      recoveryNotice.value =
        e.status === 403
          ? '权限已变化，无法恢复此回答，请新建会话。'
          : '暂时无法确认原回答状态，请稍后检查；不会自动重发。'
      if (e.status === 403 || e.status === 401) {
        messages.value = []
        unknownRequest.value = true
        recovery.value = null
        error.value = ''
      }
    }
  } finally {
    recovering.value = false
    if (!disposed && stillRunning && recovery.value === pending && attempt < 2) {
      recoveryTimer = setTimeout(() => recoverAnswer(attempt + 1), 1000 * (attempt + 1))
    }
  }
}

onMounted(() => {
  const pending = messages.value.findLast((item) => item.role === 'assistant' && item.status === 'streaming')
  if (pending?.messageId && session.value) {
    recovery.value = { sessionId: session.value, messageId: pending.messageId, answer: pending }
    recoverAnswer()
  }
})

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
  if (!question || loading.value || recovery.value || unknownRequest.value) return
  lastQuestion.value = question
  error.value = ''
  recoveryNotice.value = ''
  messages.value.push({ role: 'user', text: question })
  input.value = ''
  const answer = reactive({
    role: 'assistant',
    text: '',
    sources: [],
    steps: [],
    progress: '正在准备任务',
    loading: true,
    tokens: null
  })
  messages.value.push(answer)
  loading.value = true
  controller = new AbortController()
  await scrollToBottom()
  // 已有会话就接上去，否则让服务端在流式开始时建一个（date: start 事件带回它的 id）
  try {
    // 财务范围只认服务端记录 + 当前选择的口径（请求头自述一律不算，见后端注释）
    await streamAgent(
      question,
      session.value,
      {
        signal: controller.signal,
        onProgress: () => {
          answer.progress = '正在查询证据或整理结论…'
        },
        onStep: (step) => {
          const index = answer.steps.findIndex((item) => item.step === step.step)
          if (index < 0) answer.steps.push(step)
          else answer.steps[index] = step
          scrollToBottom()
        },
        onStart: (payload) => {
          answer.messageId = payload?.message_id
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
          answer.tokens = payload?.usage_complete === false ? null : payload?.tokens || null
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
      },
      props.alertId
    )
  } catch (e) {
    answer.loading = false
    if (e.name === 'AbortError') answer.stopped = true
    if (e.name !== 'AbortError' && !disposed) {
      error.value = e.message || '连接中断'
      if (answer.messageId && session.value) {
        recovery.value = { sessionId: session.value, messageId: answer.messageId, answer }
        await recoverAnswer()
      } else if (!e.status) {
        unknownRequest.value = true
        recoveryNotice.value = '尚未收到回答编号，无法确认服务是否已开始。请先查看会话记录，避免重复提交。'
      }
    }
  } finally {
    answer.loading = false
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
    if (id) await stopAgent(id)
  } catch (e) {
    error.value = e.message || '停止失败'
  }
  controller?.abort()
  if (recovery.value) await recoverAnswer()
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
        <span v-if="props.full" class="muted">依据授权数据调用工具分析，结论与业务原因需复核</span>
        <span v-else>支持流式问答</span>
        <button
          v-if="props.full && messages.length"
          class="btn"
          :disabled="loading || Boolean(recovery)"
          @click="clearSession"
        >
          清空会话
        </button>
        <router-link v-if="!props.full" class="btn" to="/agent">专属页面 →</router-link>
      </div>
    </div>

    <div ref="listRef" class="agent-messages">
      <div v-if="!messages.length" class="agent-empty">
        <div v-if="props.full" class="agent-welcome-icon" aria-hidden="true">✦</div>
        <h3 v-if="props.full">从一个财务问题开始</h3>
        <p v-if="props.full">查询指标 · 对比明细 · 查阅制度</p>
        <p v-if="props.examples.length">试试这些问题：</p>
        <p v-else>可以问我：“本月费用异常在哪里？”</p>
        <div v-if="props.examples.length" class="agent-examples">
          <button
            v-for="example in props.examples"
            :key="example"
            class="btn"
            :disabled="loading || Boolean(recovery) || unknownRequest"
            @click="ask(example)"
          >
            {{ example }}
          </button>
        </div>
      </div>

      <div v-for="(message, index) in messages" :key="index" class="agent-message" :class="message.role">
        <span class="agent-avatar">{{ message.role === 'user' ? '我' : 'AI' }}</span>
        <div class="agent-bubble">
          <details v-if="message.steps?.length" class="agent-steps" :open="message.loading">
            <summary>
              {{ message.loading ? '正在分析' : '执行记录' }} · {{ message.steps.length }} 个查询步骤
            </summary>
            <div v-for="step in message.steps" :key="step.step">
              {{ TOOL_LABELS[step.tool] || '工具检查' }} ·
              {{ step.status === 'ok' ? '完成' : step.status === 'running' ? '执行中' : '未完成'
              }}<span v-if="step.duration_ms != null"> · {{ step.duration_ms }} ms</span>
              <p v-if="step.summary !== '查询完成'">{{ step.summary }}</p>
            </div>
          </details>
          <!-- 模型回答按 Markdown 渲染（内部先转义再解析），用户提问保持纯文本 -->
          <template v-if="message.text && message.role === 'assistant'">
            <div class="md-body" v-html="renderMarkdown(message.text)"></div>
          </template>
          <template v-else-if="message.text">{{ message.text }}</template>
          <span v-else-if="message.loading" class="muted">{{ message.progress }}</span>
          <span v-if="message.loading">▋</span>
          <span v-if="message.stopped" class="muted stopped-note"
            >（这条回答被停止，只保留已生成的部分）</span
          >
          <p v-if="message.interrupted" class="muted">连接或服务已中断，以上仅为已保存内容。</p>
          <p v-if="message.error" class="muted">{{ message.error }}</p>
          <div v-if="message.sources && message.sources.length" class="sources">
            依据：{{ sourceText(message.sources) }}
          </div>
          <AgentEvidence :snapshots="(message.steps || []).map((step) => step.evidence).filter(Boolean)" />
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
      <button v-if="!recovery && !unknownRequest" class="btn" :disabled="loading" @click="retry">重试</button>
    </div>
    <div v-if="recoveryNotice" class="recovery-notice" role="status">
      {{ recoveryNotice }}
      <div v-if="recovery">
        <button class="btn" :disabled="recovering" @click="recoverAnswer()">
          {{ recovering ? '正在检查…' : '检查已保存结果' }}</button
        ><button class="btn" :disabled="recovering" @click="stop">停止生成</button>
      </div>
    </div>

    <div class="agent-input-row">
      <input
        v-model="input"
        :disabled="loading || Boolean(recovery) || unknownRequest"
        aria-label="财务分析问题"
        placeholder="输入问题，例如：哪些部门支出最高？"
        @keyup.enter="sendMessage"
      />
      <button
        class="btn primary"
        :disabled="Boolean(recovery) || unknownRequest"
        @click="loading ? stop() : sendMessage()"
      >
        {{ loading ? '停止' : '发送' }}
      </button>
    </div>
    <p class="composer-note">Enter 发送 · 回答请结合数据来源复核</p>
  </section>
</template>
