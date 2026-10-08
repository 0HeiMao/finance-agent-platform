<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const sidebarOpen = ref(false)

const channels = ref([])
const kinds = ref({})
const minLevels = ref({})
const levels = ref({})
const departments = ref([])
const stats = ref(null)
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
// 操作反馈走页面内的提示条，不用 ElMessage：浮层在组件测试的无 DOM 环境里会崩
const notice = ref('')
const noticeKind = ref('ok')

function say(message, kind = 'ok') {
  notice.value = message
  noticeKind.value = kind
}

// 草稿按通道 id 存：一页里同时改几条通道时，按下标存会在重新读取后错行
const drafts = ref({})
const saving = ref(0)
const testing = ref(0)
const clearing = ref(0)
const removing = ref(0)
const creating = ref(false)

const deliveries = ref([])
const deliveryTotal = ref(0)
const deliveryStats = ref(null)
const statusLabels = ref({})
const deliveryLevels = ref({})
const deliveryLoading = ref(false)
const deliveryError = ref('')
const retrying = ref(0)
const notifying = ref(false)
const notifyPeriod = ref('')

const filters = reactive({ status: '', level: '', period: '', channel_id: '' })

const form = reactive({
  name: '',
  kind: 'webhook',
  target: '',
  min_level: 'low',
  department: '',
  enabled: true,
  secret: ''
})

const enabledCount = computed(() => channels.value.filter((channel) => channel.enabled).length)
const pendingCount = computed(() =>
  channels.value.reduce((sum, channel) => sum + Number(channel.counts?.pending || 0), 0)
)

function draftOf(channel) {
  return {
    target: channel.target,
    min_level: channel.min_level,
    department: channel.department || '',
    enabled: channel.enabled,
    // 密钥只写不读：接口从不回传它，所以草稿永远是空串 = 「这一次不改密钥」
    secret: ''
  }
}

function syncDrafts() {
  drafts.value = Object.fromEntries(channels.value.map((channel) => [channel.id, draftOf(channel)]))
}

/**
 * 下拉框一律 `:value` + `@change`，不用 `v-model`。
 *
 * `v-model` 在原生 select 上会走 runtime-dom 的 vModelSelect 指令，它挂载时直接读
 * `el.options`，还会用 `addEventListener` 自己绑 change —— 组件测试的自定义渲染器没有
 * 这两样东西（节点树里没有真 DOM），`v-model` 会让整页挂不上、也点不动。ScopeSwitcher
 * 早就踩过同一个坑，这里照它的写法来。
 */
function setField(target, field, event) {
  target[field] = event.target.value
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch('/admin/alert-channels')
    const payload = await response.json()
    channels.value = payload.data || []
    kinds.value = payload.kinds || {}
    minLevels.value = payload.min_levels || {}
    levels.value = payload.levels || {}
    departments.value = payload.departments || []
    stats.value = payload.stats || null
    syncDrafts()
  } catch (err) {
    channels.value = []
    error.value = err && err.message ? err.message : '加载失败'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

async function loadDeliveries() {
  deliveryLoading.value = true
  deliveryError.value = ''
  try {
    const query = new URLSearchParams()
    if (filters.status) query.set('status', filters.status)
    if (filters.level) query.set('level', filters.level)
    if (filters.period.trim()) query.set('period', filters.period.trim())
    if (filters.channel_id) query.set('channel_id', filters.channel_id)
    query.set('limit', '50')
    const response = await apiFetch(`/admin/alert-deliveries?${query.toString()}`)
    const payload = await response.json()
    deliveries.value = payload.data || []
    deliveryTotal.value = payload.total || 0
    deliveryStats.value = payload.stats || null
    statusLabels.value = payload.status_labels || {}
    deliveryLevels.value = payload.levels || {}
  } catch (err) {
    deliveries.value = []
    deliveryError.value = err && err.message ? err.message : '台账加载失败'
  } finally {
    deliveryLoading.value = false
  }
}

async function reloadAll() {
  await Promise.all([load(), loadDeliveries()])
}

async function save(channel) {
  const draft = drafts.value[channel.id]
  const body = {
    target: draft.target,
    min_level: draft.min_level,
    department: draft.department,
    enabled: draft.enabled
  }
  // secret 留空 = 不改：接口把空串当「清空」，所以只有真填了才带上这个字段
  if (draft.secret) body.secret = draft.secret
  saving.value = channel.id
  try {
    const response = await apiFetch(`/admin/alert-channels/${channel.id}`, {
      method: 'PUT',
      body: JSON.stringify(body)
    })
    const updated = await response.json()
    Object.assign(channel, updated)
    drafts.value[channel.id] = draftOf(channel)
    say(`「${channel.name}」已保存`)
  } catch (err) {
    say(err && err.message ? err.message : '保存失败', 'bad')
  } finally {
    saving.value = 0
  }
}

async function clearSecret(channel) {
  if (!window.confirm(`清掉「${channel.name}」的签名密钥？之后这个通道发出的 webhook 不再带签名头。`)) return
  clearing.value = channel.id
  try {
    const response = await apiFetch(`/admin/alert-channels/${channel.id}`, {
      method: 'PUT',
      body: JSON.stringify({ secret: '' })
    })
    const updated = await response.json()
    Object.assign(channel, updated)
    drafts.value[channel.id] = draftOf(channel)
    say('签名密钥已清空')
  } catch (err) {
    say(err && err.message ? err.message : '清空失败', 'bad')
  } finally {
    clearing.value = 0
  }
}

async function test(channel) {
  testing.value = channel.id
  try {
    const response = await apiFetch(`/admin/alert-channels/${channel.id}/test`, { method: 'POST' })
    const result = await response.json()
    // 测试失败也回 200：发不出去是「通道的问题」，不是「请求的问题」，所以看 ok 而不是看状态码
    if (result.channel) Object.assign(channel, result.channel)
    if (result.ok) say(`「${channel.name}」连通，用时 ${result.duration_ms} 毫秒`)
    else say(`「${channel.name}」发不出去：${result.detail || '未知原因'}`, 'bad')
  } catch (err) {
    say(err && err.message ? err.message : '测试失败', 'bad')
  } finally {
    testing.value = 0
  }
}

async function remove(channel) {
  if (!window.confirm(`删除通道「${channel.name}」？已经发出去的投递台账会保留。`)) return
  removing.value = channel.id
  try {
    await apiFetch(`/admin/alert-channels/${channel.id}`, { method: 'DELETE' })
    say(`已删除「${channel.name}」`)
    await reloadAll()
  } catch (err) {
    say(err && err.message ? err.message : '删除失败', 'bad')
  } finally {
    removing.value = 0
  }
}

async function create() {
  if (!form.name.trim()) {
    say('通道要有个名字，否则台账里认不出是谁发的', 'bad')
    return
  }
  if (!form.target.trim()) {
    say(form.kind === 'email' ? '至少要写一个收件人' : '要填 webhook 地址', 'bad')
    return
  }
  creating.value = true
  try {
    await apiFetch('/admin/alert-channels', { method: 'POST', body: JSON.stringify({ ...form }) })
    say('通道已建立；历史未发送的预警可以点下方「补发通知」补上')
    form.name = ''
    form.target = ''
    form.secret = ''
    await reloadAll()
  } catch (err) {
    say(err && err.message ? err.message : '新增失败', 'bad')
  } finally {
    creating.value = false
  }
}

async function retry(delivery) {
  retrying.value = delivery.id
  try {
    const response = await apiFetch(`/admin/alert-deliveries/${delivery.id}/retry`, { method: 'POST' })
    const payload = await response.json()
    if (payload.data) Object.assign(delivery, payload.data)
    say(payload.queued ? '已重新排队，稍后自动重发' : '这条已经不需要重发了')
    await reloadAll()
  } catch (err) {
    say(err && err.message ? err.message : '重试失败', 'bad')
  } finally {
    retrying.value = 0
  }
}

async function notify() {
  notifying.value = true
  try {
    const response = await apiFetch('/admin/alerts/notify', {
      method: 'POST',
      body: JSON.stringify({ period: notifyPeriod.value.trim() })
    })
    const payload = await response.json()
    const created = payload.created || 0
    const queued = payload.queued || 0
    if (!created) say('没有需要补发的预警：要么都已发过，要么没有启用中的通道')
    else say(`新建 ${created} 条投递，其中 ${queued} 条已排队`)
    await reloadAll()
  } catch (err) {
    say(err && err.message ? err.message : '补发失败', 'bad')
  } finally {
    notifying.value = false
  }
}

onMounted(reloadAll)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>预警外发</h1>
            <p>把预警推到 webhook 或邮箱；发不出去会自动退避重试，实在发不出去的可以在台账里人工重发。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="reloadAll">
            {{ loading ? '加载中…' : '↻ 重新读取' }}
          </button>
          <router-link class="btn" to="/admin">返回管理概览</router-link>
        </div>
      </header>

      <section v-if="notice" class="state-box" :class="noticeKind === 'bad' ? 'error-state' : 'notice'">
        {{ notice }}
      </section>

      <section class="page-card">
        <h2>外发通道</h2>
        <p class="muted section-hint">
          共 {{ channels.length }} 条，启用中 {{ enabledCount }} 条，还有 {{ pendingCount }} 条投递在等发送。
          <template v-if="stats"
            >累计 {{ stats.sent || 0 }} 条已送达、{{ stats.failed || 0 }} 条已失败。</template
          >
          级别门槛是「最低发到哪一级」：选「中及以上」就不会发低级预警。通道限定部门后，只发该部门的预警。
        </p>

        <section v-if="error" class="state-box error-state">
          <strong>通道加载失败</strong>
          <p>{{ error }}</p>
          <button class="btn" @click="reloadAll">重试</button>
        </section>

        <div v-else-if="loading && !channels.length" class="muted">正在加载…</div>

        <div v-else-if="!channels.length" class="state-box empty-state">
          <strong>还没有配置任何外发通道</strong>
          <p>预警生成后只会留在这个平台里。下面建一条通道，再点「补发通知」就能把待处理的预警推出去。</p>
        </div>

        <div v-else class="table-wrap">
          <table class="channel-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>目标</th>
                <th>级别门槛</th>
                <th>部门</th>
                <th>启用</th>
                <th>最近结果</th>
                <th>台账</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="channel in channels" :key="channel.id">
                <td>
                  <strong>{{ channel.name }}</strong>
                  <small class="muted">{{ channel.has_secret ? '已配签名密钥' : '未配签名密钥' }}</small>
                </td>
                <td>{{ channel.kind_label }}</td>
                <td>
                  <input v-model="drafts[channel.id].target" class="input wide" type="text" />
                </td>
                <td>
                  <select
                    :value="drafts[channel.id].min_level"
                    class="select mini"
                    @change="setField(drafts[channel.id], 'min_level', $event)"
                  >
                    <option v-for="(label, name) in minLevels" :key="name" :value="name">{{ label }}</option>
                  </select>
                </td>
                <td>
                  <select
                    :value="drafts[channel.id].department"
                    class="select mini"
                    @change="setField(drafts[channel.id], 'department', $event)"
                  >
                    <option value="">不限部门</option>
                    <option v-for="name in departments" :key="name" :value="name">{{ name }}</option>
                  </select>
                </td>
                <td><input v-model="drafts[channel.id].enabled" type="checkbox" /></td>
                <td>
                  <span v-if="channel.last_result === 'ok'" class="ok">成功</span>
                  <span v-else-if="channel.last_result" class="bad">失败</span>
                  <span v-else class="muted">还没发过</span>
                  <small class="muted">{{ channel.last_sent_at || '—' }}</small>
                  <small v-if="channel.last_error" class="muted err-text">{{ channel.last_error }}</small>
                </td>
                <td>
                  <small class="muted">待发 {{ channel.counts?.pending || 0 }}</small>
                  <small class="muted">已送达 {{ channel.counts?.sent || 0 }}</small>
                  <small class="muted">已失败 {{ channel.counts?.failed || 0 }}</small>
                </td>
                <td>
                  <input
                    v-model="drafts[channel.id].secret"
                    class="input mini"
                    type="password"
                    placeholder="留空=不改密钥"
                  />
                  <button class="btn" :disabled="saving === channel.id" @click="save(channel)">
                    {{ saving === channel.id ? '保存中…' : '保存' }}
                  </button>
                  <button class="btn" :disabled="testing === channel.id" @click="test(channel)">
                    {{ testing === channel.id ? '发送中…' : '测试' }}
                  </button>
                  <button
                    class="btn"
                    :disabled="!channel.has_secret || clearing === channel.id"
                    :title="channel.has_secret ? '清掉签名密钥' : '这条通道没配密钥'"
                    @click="clearSecret(channel)"
                  >
                    清空密钥
                  </button>
                  <button class="btn danger" :disabled="removing === channel.id" @click="remove(channel)">
                    {{ removing === channel.id ? '删除中…' : '删除' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p class="muted section-hint">
          webhook 用 POST 发 JSON，正文形如
          <code>{"event":"alert.created","alert":{…}}</code>；配了签名密钥就带
          <code>X-Finance-Signature: sha256=…</code>（对「时间戳.正文」做 HMAC-SHA256）。平台只发 http/https、
          地址里不能带用户名口令、不跟随跳转、5
          秒超时——内部网关地址是允许的，所以别把通道指向公网上的陌生地址。
        </p>
      </section>

      <section class="page-card">
        <h2>新增通道</h2>
        <p class="muted section-hint">
          新建不会自动把历史预警补发出去，建完请点下面的「补发通知」。邮件通道需要后端配好
          <code>ALERT_SMTP_HOST</code> / <code>ALERT_MAIL_FROM</code>，否则测试会直接告诉你「没配 SMTP」。
        </p>

        <div class="form-grid">
          <label class="field">
            <span>名称</span>
            <input v-model="form.name" class="input" type="text" placeholder="例如：财务群机器人" />
          </label>
          <label class="field">
            <span>类型</span>
            <select :value="form.kind" class="select" @change="setField(form, 'kind', $event)">
              <option v-for="(label, name) in kinds" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field">
            <span>{{ form.kind === 'email' ? '收件人（逗号分隔）' : 'Webhook 地址' }}</span>
            <input
              v-model="form.target"
              class="input"
              type="text"
              :placeholder="
                form.kind === 'email' ? 'fin@example.com, ops@example.com' : 'http://gateway.internal/hook'
              "
            />
          </label>
          <label class="field">
            <span>级别门槛</span>
            <select :value="form.min_level" class="select" @change="setField(form, 'min_level', $event)">
              <option v-for="(label, name) in minLevels" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field">
            <span>限定部门（可留空）</span>
            <select :value="form.department" class="select" @change="setField(form, 'department', $event)">
              <option value="">不限部门</option>
              <option v-for="name in departments" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
          <label class="field">
            <span>签名密钥（可留空，只写不读）</span>
            <input v-model="form.secret" class="input" type="password" placeholder="建立后接口不会再回传它" />
          </label>
        </div>

        <div class="form-actions">
          <label class="field inline">
            <input v-model="form.enabled" type="checkbox" />
            <span>建立后立即启用</span>
          </label>
          <button class="btn primary" :disabled="creating" @click="create">
            {{ creating ? '新增中…' : '新增通道' }}
          </button>
        </div>
      </section>

      <section class="page-card">
        <h2>投递台账</h2>
        <p class="muted section-hint">
          台账按「期间 + 部门 + 预警类型 + 指标 + 通道」去重：重算会把预警行删掉重建，如果用行号去重，
          每重算一次就会把同样的通知再轰一遍。共 {{ deliveryTotal }} 条，
          <template v-if="deliveryStats">
            待发 {{ deliveryStats.pending || 0 }}、已送达 {{ deliveryStats.sent || 0 }}、已失败
            {{ deliveryStats.failed || 0 }}。
          </template>
        </p>

        <div class="filter-row">
          <label class="field inline">
            <span>状态</span>
            <select :value="filters.status" class="select mini" @change="setField(filters, 'status', $event)">
              <option value="">全部</option>
              <option v-for="(label, name) in statusLabels" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field inline">
            <span>级别</span>
            <select :value="filters.level" class="select mini" @change="setField(filters, 'level', $event)">
              <option value="">全部</option>
              <option v-for="(label, name) in deliveryLevels" :key="name" :value="name">{{ label }}</option>
            </select>
          </label>
          <label class="field inline">
            <span>通道</span>
            <select
              :value="filters.channel_id"
              class="select mini"
              @change="setField(filters, 'channel_id', $event)"
            >
              <option value="">全部</option>
              <option v-for="channel in channels" :key="channel.id" :value="channel.id">
                {{ channel.name }}
              </option>
            </select>
          </label>
          <label class="field inline">
            <span>期间</span>
            <input v-model="filters.period" class="input mini" type="text" placeholder="YYYY-MM" />
          </label>
          <button class="btn" :disabled="deliveryLoading" @click="loadDeliveries">
            {{ deliveryLoading ? '筛选中…' : '筛选' }}
          </button>
          <span class="spacer"></span>
          <label class="field inline">
            <span>补发期间（留空=全部）</span>
            <input v-model="notifyPeriod" class="input mini" type="text" placeholder="YYYY-MM" />
          </label>
          <button class="btn primary" :disabled="notifying" @click="notify">
            {{ notifying ? '补发中…' : '补发通知' }}
          </button>
        </div>

        <section v-if="deliveryError" class="state-box error-state">
          <strong>台账加载失败</strong>
          <p>{{ deliveryError }}</p>
          <button class="btn" @click="loadDeliveries">重试</button>
        </section>

        <div v-else-if="deliveryLoading && !deliveries.length" class="muted">正在加载…</div>

        <div v-else-if="!deliveries.length" class="state-box empty-state">
          <strong>台账里还没有记录</strong>
          <p>预警重算或导入之后，命中启用通道的预警会在这里留下一条投递记录。</p>
        </div>

        <div v-else class="table-wrap">
          <table class="delivery-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>期间</th>
                <th>预警</th>
                <th>通道</th>
                <th>级别</th>
                <th>状态</th>
                <th>尝试</th>
                <th>响应</th>
                <th>失败原因</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="delivery in deliveries" :key="delivery.id">
                <td>
                  <small class="muted">{{ delivery.sent_at || delivery.created_at || '—' }}</small>
                </td>
                <td>{{ delivery.period }}</td>
                <td>
                  <strong>{{ delivery.alert_type }}</strong>
                  <small class="muted"
                    >{{ delivery.department || '全公司' }} · {{ delivery.metric_name }}</small
                  >
                </td>
                <td>
                  {{ delivery.channel_name || '（通道已删除）' }}
                  <small class="muted">{{ delivery.channel_kind_label }}</small>
                </td>
                <td>
                  <span class="level" :class="delivery.level">{{ delivery.level_label }}</span>
                </td>
                <td>
                  <span class="chip" :class="`chip-${delivery.status}`">{{ delivery.status_label }}</span>
                </td>
                <td>{{ delivery.attempts }}</td>
                <td>{{ delivery.response_status || '—' }}</td>
                <td>
                  <small class="muted">{{ delivery.last_error || '—' }}</small>
                </td>
                <td>
                  <button
                    class="btn"
                    :disabled="retrying === delivery.id || delivery.status === 'sent'"
                    :title="delivery.status === 'sent' ? '已经送达，不需要重发' : '立刻重发这一条'"
                    @click="retry(delivery)"
                  >
                    {{ retrying === delivery.id ? '排队中…' : '重发' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
h2 {
  font-size: 16px;
  margin: 0 0 6px;
}
.section-hint {
  font-size: 12px;
  margin: 0 0 12px;
}
.channel-table,
.delivery-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 1040px;
}
.channel-table th,
.delivery-table th {
  text-align: left;
  font-size: 13px;
  color: #64748b;
  font-weight: 500;
  padding: 6px 10px 6px 0;
}
.channel-table td,
.delivery-table td {
  padding: 8px 10px 8px 0;
  vertical-align: top;
  border-top: 1px solid #eef1f6;
  font-size: 13px;
}
.channel-table small,
.delivery-table small {
  display: block;
  font-size: 11px;
}
.input.mini,
.select.mini {
  width: 108px;
}
.input.wide {
  width: 100%;
  min-width: 180px;
}
.channel-table .btn {
  margin-right: 6px;
}
.err-text {
  color: #b91c1c;
  max-width: 220px;
  word-break: break-all;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #475569;
}
.field.inline {
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
.form-actions,
.filter-row {
  margin-top: 14px;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}
.filter-row .spacer {
  flex: 1;
}
.btn.danger {
  border-color: #fecaca;
  color: #b91c1c;
}
.btn.primary {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #fff;
}
.bad {
  color: #b91c1c;
  font-weight: 600;
}
.chip {
  display: inline-block;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #e2e8f0;
  color: #475569;
}
.chip-sent {
  background: #dcfce7;
  color: #15803d;
}
.chip-pending {
  background: #fef3c7;
  color: #92400e;
}
.chip-failed {
  background: #fee2e2;
  color: #b91c1c;
}
</style>
