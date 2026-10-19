import { reactive } from 'vue'

export const toasts = reactive([])

export function toast(msg, type = 'info', duration = 4000) {
  const id = Date.now() + Math.random()
  toasts.push({ id, msg, type })
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id)
    if (i >= 0) toasts.splice(i, 1)
  }, duration)
}

export function errToast(e) {
  toast(String(e?.message || e), 'error', 6000)
}
