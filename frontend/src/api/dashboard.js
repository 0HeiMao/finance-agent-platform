import { apiFetch } from './client'
import { withScope } from './scope'

export async function getDashboard(period = 'year', signal) {
  // 口径由 withScope 统一附加：管理员在看板上切到某个部门后，这里取到的就是那个部门的数。
  const response = await apiFetch(withScope(`/dashboard?period=${encodeURIComponent(period)}`), { signal })
  return response.json()
}
