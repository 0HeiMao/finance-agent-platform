import { ref } from 'vue'
import { apiFetch } from './client'

/**
 * 当前登录用户的轻量共享状态。
 *
 * 为什么不用 Pinia：项目没装 Pinia，而这里只需要"整站只取一次 `/auth/me`、结果共享"。
 * 用一个模块级 ref + 进行中的 Promise 缓存就够了，侧边栏和路由守卫都读同一份，不会重复打接口。
 * 权限口径与后端一致：**部门为空 = 不受限制 = 财务管理员**。
 */
const user = ref(null)
let pending = null

export function currentUser() {
  return user
}

export function isAdmin() {
  return Boolean(user.value?.unrestricted)
}

export async function loadCurrentUser(force = false) {
  if (user.value && !force) return user.value
  if (!pending || force) {
    pending = apiFetch('/auth/me')
      .then((response) => response.json())
      .then((me) => {
        user.value = me
        return me
      })
      .catch(() => {
        // 401 已由 apiFetch 统一处理（清会话跳 /login），这里只把状态置空，页面按未登录渲染
        user.value = null
        return null
      })
      .finally(() => {
        pending = null
      })
  }
  return pending
}

export function clearCurrentUser() {
  user.value = null
  pending = null
}
