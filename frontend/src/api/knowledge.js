import { apiFetch } from './client'

export async function listKnowledge(params, signal) {
  return (await apiFetch(`/admin/knowledge?${params}`, { signal })).json()
}

export async function knowledgeWriteScope(signal) {
  return (await apiFetch('/rag/write-scope', { signal })).json()
}

export async function uploadKnowledge(file, department, publishPublic, signal) {
  const body = new FormData()
  body.append('file', file)
  body.append('publish_public', String(publishPublic))
  if (department) body.append('department', department)
  return (await apiFetch('/rag/documents/upload', { method: 'POST', body, signal })).json()
}

export async function deleteKnowledge(source, signal) {
  return (
    await apiFetch(`/admin/knowledge?source=${encodeURIComponent(source)}`, {
      method: 'DELETE',
      signal
    })
  ).json()
}
