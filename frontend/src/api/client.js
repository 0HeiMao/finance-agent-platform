const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'

/**
 * 把后端错误体变成一句人话。
 *
 * FastAPI 的 422 是 `detail: [{loc,msg,type}, …]`，直接 `new Error(detail)` 会得到
 * 「导出失败：[object Object]」——等于没告诉用户哪里错了。
 */
export function errorMessage(detail, fallback = '请求失败') {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) =>
        typeof item === 'string'
          ? item
          : item?.msg
            ? `${(item.loc || []).slice(1).join('.')}：${item.msg}`
            : ''
      )
      .filter(Boolean)
    if (parts.length) return parts.join('；')
  }
  if (detail && typeof detail === 'object' && typeof detail.msg === 'string') return detail.msg
  return fallback
}

export async function apiFetch(path, options = {}) {
  const token = localStorage.getItem('access_token')
  const headers = new Headers(options.headers || {})
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers })
  if (response.status === 401) {
    localStorage.removeItem('access_token')
    window.location.assign('/login')
    throw new Error('登录已过期')
  }
  if (!response.ok) {
    const error = new Error(errorMessage((await response.json().catch(() => ({}))).detail))
    error.status = response.status
    throw error
  }
  return response
}

export async function readSSE(path, body, { onStart, onDelta, onDone, onError, onStep, onProgress, signal }) {
  const response = await apiFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
    headers: { Accept: 'text/event-stream' }
  })
  const reader = response.body.getReader()
  const cancel = () => {
    reader.cancel().catch(() => {})
  }
  signal?.addEventListener('abort', cancel, { once: true })
  const decoder = new TextDecoder()
  let buffer = ''
  let completed = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (signal?.aborted) throw new DOMException('已停止', 'AbortError')
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''
      for (const raw of events) {
        const event = raw.match(/^event:\s*(.+)$/m)?.[1] || 'delta'
        const data = raw.match(/^data:\s*(.+)$/m)?.[1]
        if (!data) continue
        const payload = JSON.parse(data)
        if (event === 'start') onStart?.(payload)
        else if (event === 'step') onStep?.(payload)
        else if (event === 'progress') onProgress?.(payload)
        else if (event === 'delta') onDelta?.(payload.text || '')
        else if (event === 'done') {
          completed = true
          onDone?.(payload)
        } else if (event === 'error') {
          completed = true
          onError?.(payload)
        }
      }
    }
    if (!completed) throw new Error('连接提前结束，请重试或查看会话记录')
  } catch (error) {
    if (error.name !== 'AbortError') onError?.({ message: error.message || '流式连接中断' })
    throw error
  } finally {
    signal?.removeEventListener('abort', cancel)
    reader.releaseLock()
  }
}
