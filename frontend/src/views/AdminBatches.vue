<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'
import {
  describeResult,
  durationText,
  formatTime,
  periodsText,
  resultText,
  statusLabel
} from '../utils/format'

const PAGE_SIZE = 50
// 后台任务轮询间隔：重算七个期间通常几秒内结束，800ms 足够快地给出反馈，也不至于压满后端。
const JOB_POLL_MS = 800

const sidebarOpen = ref(false)
const batches = ref([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busy = ref('')
const confirmBatch = ref('')
const jobs = ref([])
const jobTotal = ref(0)
// 正在后台撤销的批次号：这一行的按钮要变成"撤销中…"且不可再点
const pendingBatch = ref('')
let pollTimer = null

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiFetch(`/admin/batches?limit=${PAGE_SIZE}&offset=${offset.value}`)
    const payload = await response.json()
    batches.value = payload.data
    total.value = payload.total || 0
  } catch (e) {
    error.value = `读取导入批次失败：${e.message}`
    batches.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function loadJobs() {
  try {
    const response = await apiFetch('/admin/jobs?limit=5')
    const payload = await response.json()
    jobs.value = payload.data
    jobTotal.value = payload.total || 0
  } catch {
    // 任务列表读不到不影响撤销主体，不再叠一条错误把人吓一跳
    jobs.value = []
  }
}

function turn(next) {
  offset.value = next
  load()
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollJob(jobId) {
  try {
    const response = await apiFetch(`/admin/jobs/${jobId}`)
    const job = await response.json()
    if (job.status === 'queued' || job.status === 'running') {
      return
    }
    stopPolling()
    pendingBatch.value = ''
    if (job.status === 'succeeded') {
      notice.value = describeResult(job)
      // 撤销的是本页最后一条时回退一页，避免任务完成后停在一张空表上
      if (batches.value.length === 1 && offset.value >= PAGE_SIZE) {
        offset.value -= PAGE_SIZE
      }
    } else {
      // 后台任务的失败原因必须露出来：否则用户只看到"撤销没生效"，不知道该改什么
      error.value = `后台任务 #${job.id} 失败：${job.error || '原因未知'}`
    }
    await Promise.all([load(), loadJobs()])
  } catch (e) {
    stopPolling()
    pendingBatch.value = ''
    error.value = `查询后台任务失败：${e.message}`
  }
}

function watchJob(jobId) {
  stopPolling()
  pollJob(jobId)
  pollTimer = setInterval(() => pollJob(jobId), JOB_POLL_MS)
}

async function removeBatch(batch) {
  busy.value = batch.batch
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch('/admin/jobs/batch-revert', {
      method: 'POST',
      body: JSON.stringify({ batch: batch.batch })
    })
    const job = await response.json()
    pendingBatch.value = batch.batch
    confirmBatch.value = ''
    notice.value = `已提交后台任务 #${job.id}：正在删除 ${batch.rows} 行明细并重算受影响期间，可以离开本页。`
    loadJobs()
    watchJob(job.id)
  } catch (e) {
    error.value = `撤销失败：${e.message}`
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  load()
  loadJobs()
})
onUnmounted(stopPolling)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>导入批次</h1>
            <p>
              每次导入按文件内容哈希记为一个批次；撤销会先删该批明细，再重算受影响期间的指标与预警，重算在后台执行。
            </p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <p v-if="loading && !batches.length" class="muted">正在读取导入批次…</p>

      <section v-else-if="error && !batches.length" class="state-box error-state">
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

        <section v-if="!batches.length" class="state-box empty-state">
          <strong>还没有导入数据</strong>
          <p>先到「数据导入」上传 CSV/XLSX，或用 scripts/import_via_api.py 直接导入演示数据。</p>
          <router-link class="btn primary" to="/financial/import">去导入数据</router-link>
        </section>

        <section v-else class="page-card">
          <div class="card-head">
            <h2>共 {{ total }} 个批次</h2>
            <span>同一文件重复导入会被 409 拒绝，因此这里每个批次都是唯一内容</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>批次（前 12 位）</th>
                  <th>行数</th>
                  <th>日期范围</th>
                  <th>涉及期间</th>
                  <th>导入时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="batch in batches" :key="batch.batch">
                  <td :title="batch.batch">
                    <code>{{ batch.short }}…</code>
                  </td>
                  <td>{{ batch.rows }}</td>
                  <td>{{ batch.first_date }} ~ {{ batch.last_date }}</td>
                  <td :title="batch.periods.join('、')">{{ periodsText(batch) }}</td>
                  <td>{{ batch.imported_at || '—' }}</td>
                  <td>
                    <div class="row-actions">
                      <template v-if="pendingBatch === batch.batch">
                        <button class="btn" disabled>撤销中…</button>
                      </template>
                      <template v-else-if="confirmBatch !== batch.batch">
                        <button
                          class="btn danger"
                          :disabled="busy === batch.batch"
                          @click="confirmBatch = batch.batch"
                        >
                          撤销批次
                        </button>
                      </template>
                      <template v-else>
                        <button
                          class="btn danger"
                          :disabled="busy === batch.batch"
                          @click="removeBatch(batch)"
                        >
                          {{ busy === batch.batch ? '提交中…' : '确认撤销' }}
                        </button>
                        <button class="btn" @click="confirmBatch = ''">取消</button>
                      </template>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <PagePager
            :total="total"
            :limit="PAGE_SIZE"
            :offset="offset"
            :loading="loading"
            unit="个批次"
            @change="turn"
          />
          <p class="muted">
            撤销只删数据库里的行，`data/raw/` 下的原始文件按只读约定保留；重新导入同一文件会重新生成同批次号。
          </p>
        </section>

        <section v-if="jobs.length" class="page-card">
          <div class="card-head">
            <h2>最近的后台任务</h2>
            <span>共 {{ jobTotal }} 条（只显示最近 5 条）；重算在后台线程里跑，失败原因写在这里</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>提交人</th>
                  <th>提交时间</th>
                  <th>耗时</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="job in jobs" :key="job.id">
                  <td>#{{ job.id }}</td>
                  <td>{{ job.kind_label }}</td>
                  <td>
                    <span class="job-status" :class="job.status">{{ statusLabel(job.status) }}</span>
                  </td>
                  <td>{{ job.actor || '—' }}</td>
                  <td>{{ formatTime(job.created_at) }}</td>
                  <td>{{ durationText(job) }}</td>
                  <td :title="resultText(job)">{{ resultText(job) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="muted">
            后台任务是进程内队列：进程重启会把未完成的任务标记为失败，多副本部署需要换成外部队列。
          </p>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped>
.job-status {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: #eef3f9;
  color: #1a3a5c;
}
.job-status.running {
  background: #e6f4ff;
  color: #0958d9;
}
.job-status.succeeded {
  background: #f6ffed;
  color: #237804;
}
.job-status.failed {
  background: #fff2f0;
  color: #a8071a;
}
</style>
