import { reactive } from 'vue'
import { api } from './api.js'

export const store = reactive({
  voices: [],
  documents: [],
  projects: [],
  engines: [],
  settings: {},
  jobs: [],
  loading: false,
})

export async function refreshAll() {
  store.loading = true
  try {
    const [voices, documents, projects, engines, jobs, settings] = await Promise.all([
      api.voices().catch(() => []),
      api.documents().catch(() => []),
      api.projects().catch(() => []),
      api.engines().catch(() => []),
      api.jobs().catch(() => []),
      api.getSettings().catch(() => ({})),
    ])
    store.voices = voices
    store.documents = documents
    store.projects = projects
    store.engines = engines
    store.jobs = jobs
    store.settings = settings
  } finally {
    store.loading = false
  }
}

export function engineInfo(id) {
  return store.engines.find((e) => e.id === id) || { name: id, ok: false }
}

export function voiceName(id) {
  const v = store.voices.find((v) => v.id === id)
  return v ? v.name : '（未选择）'
}

// 轮询任务直到结束，onUpdate 回调（进度/消息/结果）
// 容错：单次网络失败（瞬时抖动/后端繁忙）不判死，指数退避静默重试；
// 连续失败才报错，且优先等待后端返回的真实 job 状态（避免 Failed to fetch 掩盖训练失败原因）。
export function pollJob(jobId, onUpdate, intervalMs = 1500) {
  let stopped = false
  let timer = null
  let failCount = 0
  const tick = async () => {
    if (stopped) return
    try {
      const j = await api.job(jobId)
      failCount = 0
      if (onUpdate) onUpdate(j)
      if (j.status === 'queued' || j.status === 'running') {
        timer = setTimeout(tick, intervalMs)
      }
    } catch (e) {
      failCount++
      if (failCount >= 5) {
        if (onUpdate) onUpdate({ status: 'error', message: `与后端服务连接中断（${e}），请确认后端仍在运行后重试` })
        return
      }
      // 瞬时网络错误：静默重试（1.5s → 3s → 6s → 12s → 15s 退避），不打断轮询
      timer = setTimeout(tick, Math.min(intervalMs * Math.pow(2, failCount), 15000))
    }
  }
  tick()
  return () => {
    stopped = true
    if (timer) clearTimeout(timer)
  }
}

export function jobText(j) {
  if (!j) return ''
  const icons = { queued: '⏳', running: '⚙️', success: '✅', error: '❌' }
  return `${icons[j.status] || ''} ${j.title} — ${j.message || ''}${j.status === 'success' ? `（${j.progress}%）` : ''}`
}
