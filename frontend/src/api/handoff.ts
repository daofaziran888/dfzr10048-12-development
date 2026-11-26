import { request } from './client'

export type HandoffScalar = string | number | boolean | null
export type HandoffRecord = Record<string, unknown>

async function postJson<T = HandoffRecord>(path: string, body: HandoffRecord = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as T
}

async function getJson<T = HandoffRecord>(path: string): Promise<T> {
  const response = await request(path)
  return (await response.json()) as T
}

export const handoffApi = {
  state: () => getJson('/api/archive-handoff/state'),
  seed: (submitLateA = true) => postJson('/api/archive-handoff/seed', { submit_late_a: submitLateA }),
  reset: (seed = true) => postJson('/api/archive-handoff/reset', { seed }),
  shelfView: (taskId = 'BCMP-HANDOFF-001') =>
    getJson(`/api/archive_shelf/handoff-view?task_id=${encodeURIComponent(taskId)}`),
  redeliveryView: (taskId = 'BCMP-HANDOFF-001') =>
    getJson(`/api/fail_redeliver/handoff-view?task_id=${encodeURIComponent(taskId)}`),
  claim: (body: HandoffRecord) => postJson('/api/fail_redeliver/try-claim', body),
  receipt: (body: HandoffRecord) => postJson('/api/archive-handoff/receipts', body),
  transfer: (body: HandoffRecord) => postJson('/api/archive-handoff/handoffs/transfer', body),
  restore: (body: HandoffRecord) => postJson('/api/archive-handoff/restore-to-task', body),
  progress: (body: HandoffRecord) => postJson('/api/archive-handoff/progress', body),
  halfDemo: () => postJson('/api/archive-handoff/demo-half', {}),
}
