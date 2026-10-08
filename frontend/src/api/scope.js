import { ref } from 'vue'

/**
 * 「以哪个口径查看」的全局选择。
 *
 * 为什么放在模块级 ref 而不是每个页面各存一份：口径是**整个会话的视角**，不是某个页面的局部状态。
 * 在看板上切到「部门 097」，再点进收入分析/报告/Agent 时必须还是 097，否则用户会拿着
 * 两个口径的数字互相比较——这正是部门权限最容易出错的地方（数字不会报错，只会悄悄不一样）。
 *
 * 空字符串 = 用账号自身的范围（不受限账号 = 全公司）。受限账号不能切换，选择器也不会出现，
 * 即使这里被写入别的值，服务端也会 403。
 */
export const activeScope = ref('')

export function setActiveScope(value) {
  activeScope.value = value || ''
}

/** 把当前口径拼进请求路径：`withScope('/dashboard?period=month')`。 */
export function withScope(path) {
  if (!activeScope.value) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}department=${encodeURIComponent(activeScope.value)}`
}

/** 当前口径的中文说明（用于「正在按部门 X 查看」这类提示）。 */
export function scopeLabel(value = activeScope.value) {
  return value ? `部门 ${value}` : '全公司'
}
