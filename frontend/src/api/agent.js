import { apiFetch, readSSE } from './client'
import { withScope } from './scope'

export async function listAgentSessions(signal) {
  return (await apiFetch('/ai/agent/sessions?limit=50', { signal })).json()
}
export async function getAgentUsage(signal) {
  return (await apiFetch('/ai/agent/usage?days=7', { signal })).json()
}
export async function createAgentSession(signal) {
  return (await apiFetch('/ai/agent/sessions', { method: 'POST', body: JSON.stringify({}), signal })).json()
}
export async function getAgentSession(id, signal) {
  return (await apiFetch(`/ai/agent/sessions/${id}`, { signal })).json()
}
export async function getAgentMessage(sessionId, messageId, signal) {
  return (await apiFetch(`/ai/agent/sessions/${sessionId}/messages/${messageId}`, { signal })).json()
}
export function deleteAgentSession(id, signal) {
  return apiFetch(`/ai/agent/sessions/${id}`, { method: 'DELETE', signal })
}

export async function getAlertContext(id, signal) {
  return (await apiFetch(`/ai/agent/alerts/${encodeURIComponent(id)}/context`, { signal })).json()
}

export function streamAgent(question, sessionId, handlers, alertId = null) {
  const path = sessionId ? `/ai/agent/chat/stream?session_id=${sessionId}` : '/ai/agent/chat/stream'
  return readSSE(
    alertId ? path : withScope(path),
    { question, mode: 'agent', ...(alertId ? { alert_id: alertId } : {}) },
    handlers
  )
}

export function stopAgent(sessionId) {
  return apiFetch(`/ai/agent/sessions/${sessionId}/stop`, { method: 'POST' })
}
