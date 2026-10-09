/**
 * 管理页共用的纯格式化函数。
 *
 * 放在这里而不是写进 .vue 的 `<script setup>` 里，是因为 .vue 需要编译器才能 import：
 * 抽成普通 ES 模块后，`node --test tests/` 不装任何依赖就能直接测（见 frontend/tests/）。
 */

export const JOB_STATUS_LABELS = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败'
}

export function statusLabel(status) {
  return JOB_STATUS_LABELS[status] || status
}

/**
 * 任务跑到第几次了。
 *
 * ⑩ 之后失败会自动重排（2/4/8…秒退避），所以「失败」不再等于「没救了」：
 * 界面必须能看出「第 1/3 次」还是「第 3/3 次」，否则人工点「重试」时无从判断。
 * 后端 `attempts` 从 0 开始计数，0 表示一次都还没跑。
 */
export function attemptsText(job) {
  if (!job) return '—'
  const attempts = Number(job.attempts ?? 0)
  if (!attempts) return '未开始'
  const max = Number(job.max_attempts ?? 0) || 3
  return `第 ${attempts}/${max} 次`
}

/** 排队中且带着下次重试时间 → 这一行正在退避等待；其它情况没有额外信息可给。 */
export function retryWaitText(job) {
  if (!job || job.status !== 'queued' || !job.next_attempt_at) return '—'
  return `约 ${formatTime(job.next_attempt_at)} 重试`
}

/**
 * 谁正在跑这条任务（⑱）。
 *
 * 多副本部署下同一个任务只能有一个执行者，把那个进程记在界面上才解释得清「任务一直卡在运行中」：
 * 记下来的主机名 + 进程号就是该去翻的那份日志。只有 `running` 才显示 —— 跑完/判失败/重新排队的
 * 任务没有执行者，显示一个早就结束的进程只会让人以为它还在跑。
 */
export function ownerText(job) {
  if (!job || job.status !== 'running' || !job.worker_id) return ''
  return `运行进程：${job.worker_id}`
}

export function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

export function durationText(job) {
  if (!job || !job.started_at) return '—'
  const started = new Date(job.started_at)
  const finished = job.finished_at ? new Date(job.finished_at) : new Date()
  const seconds = (finished - started) / 1000
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  return `${seconds.toFixed(1)} 秒`
}

export function resultText(job) {
  if (!job) return '—'
  if (job.status === 'failed') return job.error || '失败原因未记录'
  if (job.status !== 'succeeded') return '—'
  const result = job.result || {}
  // 重跑时上次已经删干净了（⑩）：说清楚"没有行可删"不是失败
  if (result.already_reverted) return '该批次此前已被撤销（本次 0 行）'
  const parts = [`删除 ${result.deleted_rows ?? 0} 行`]
  if ((result.recalculated || []).length) parts.push(`重算 ${result.recalculated.length} 期`)
  if ((result.cleared || []).length) parts.push(`清空 ${result.cleared.length} 期`)
  return parts.join(' · ')
}

export function describeResult(job) {
  const result = (job && job.result) || {}
  if (result.already_reverted) {
    return `后台任务 #${job.id} 已完成：该批次此前已经被撤销过，本次没有行可删。`
  }
  const parts = [`后台任务 #${job.id} 已完成：删除 ${result.deleted_rows ?? 0} 行明细`]
  if ((result.recalculated || []).length) parts.push(`重算期间：${result.recalculated.join('、')}`)
  if ((result.cleared || []).length) parts.push(`清空期间：${result.cleared.join('、')}`)
  return `${parts.join('；')}。`
}

/**
 * 涉及期间在一行里排开会把「操作」列挤出 1440 宽的视口（要横向滚动才能点到撤销），
 * 所以连续月份压成「2026-04 → 2026-10（7 期）」，不连续的只列前三个（末尾标总期数）。
 */
export function periodsText(batch) {
  const periods = (batch && batch.periods) || []
  if (!periods.length) return '—'
  if (periods.length === 1) return periods[0]
  const contiguous = periods.every((period, index) => {
    if (index === 0) return true
    const [year, month] = periods[index - 1].split('-').map(Number)
    const next = month === 12 ? `${year + 1}-01` : `${year}-${String(month + 1).padStart(2, '0')}`
    return period === next
  })
  if (contiguous) return `${periods[0]} → ${periods[periods.length - 1]}（${periods.length} 期）`
  const head = periods.slice(0, 3).join('、')
  return periods.length > 3 ? `${head} 等 ${periods.length} 期` : periods.join('、')
}

/**
 * 一个账号可以属于多个部门（并集）：输入框里用「、」「,」「，」「;」或空格分隔。
 *
 * 后端 `DepartmentInput.departments` 限 20 个，这里同步截断，避免提交后才报 422；
 * 重复项要去掉（保序）——同一个部门写两遍会让 scope 元组出现重复元素，
 * 后端 `_totals_by_months` 用 `len(scope)` 当每月的期望行数，重复会让它误判数据不完整。
 */
export const MAX_DEPARTMENTS = 20

export function parseDepartments(input) {
  const raw = Array.isArray(input) ? input : String(input ?? '').split(/[、,，;；\s]+/)
  const seen = []
  for (const item of raw) {
    const name = String(item ?? '').trim()
    if (!name || seen.includes(name)) continue
    seen.push(name)
    if (seen.length >= MAX_DEPARTMENTS) break
  }
  return seen
}

/** 展示用：`['018', '097']` → `018、097`；空值 → 空串（由调用方决定占位文案）。 */
export function departmentsText(value) {
  return parseDepartments(value).join('、')
}
