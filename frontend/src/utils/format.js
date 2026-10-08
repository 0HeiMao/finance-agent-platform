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
  const parts = [`删除 ${result.deleted_rows ?? 0} 行`]
  if ((result.recalculated || []).length) parts.push(`重算 ${result.recalculated.length} 期`)
  if ((result.cleared || []).length) parts.push(`清空 ${result.cleared.length} 期`)
  return parts.join(' · ')
}

export function describeResult(job) {
  const result = (job && job.result) || {}
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
