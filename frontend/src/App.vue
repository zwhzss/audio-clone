<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { store, refreshAll } from './store.js'
import { toasts } from './toast.js'
import VoicesView from './views/VoicesView.vue'
import DocumentsView from './views/DocumentsView.vue'
import StudioView from './views/StudioView.vue'
import ProjectsView from './views/ProjectsView.vue'
import ModelsView from './views/ModelsView.vue'
import SeparationView from './views/SeparationView.vue'
import SlicingView from './views/SlicingView.vue'
import PolishView from './views/PolishView.vue'
import DigitalHumanView from './views/DigitalHumanView.vue'
import SettingsView from './views/SettingsView.vue'

const view = ref(localStorage.getItem('ac_view') || 'studio')
const views = [
  { id: 'studio', label: '🎛️ 合成工作台' },
  { id: 'voices', label: '🎙️ 声音库' },
  { id: 'digital-human', label: '🎭 数字人' },
  { id: 'separation', label: '🎵 歌曲分离' },
  { id: 'slicing', label: '🔪 音频分片' },
  { id: 'polish', label: '🎚️ 音频润色' },
  { id: 'documents', label: '📄 文稿' },
  { id: 'projects', label: '📚 作品' },
  { id: 'models', label: '🧩 模型库' },
  { id: 'settings', label: '⚙️ 设置' },
]

function switchView(id) {
  view.value = id
  localStorage.setItem('ac_view', id)
}

watch(view, () => refreshAll())

onMounted(() => refreshAll())
</script>

<template>
  <aside class="sidebar">
    <div class="brand">🎙️ 声音克隆工坊
      <small>有声读物 · 配音 · 本地存储</small>
    </div>
    <nav>
      <div v-for="v in views" :key="v.id" class="nav-item" :class="{ active: view === v.id }" @click="switchView(v.id)">
        {{ v.label }}
      </div>
    </nav>
    <div class="sidebar-foot">
      数据全部存储在本地<br />
      引擎状态：
      <span v-for="e in store.engines" :key="e.id">
        <span :style="{ color: e.ok ? 'var(--ok)' : 'var(--muted)' }">{{ e.name.split('（')[0] }}</span>
        <span v-if="e !== store.engines[store.engines.length - 1]"> · </span>
      </span>
    </div>
  </aside>
  <main class="main">
    <StudioView v-if="view === 'studio'" />
    <VoicesView v-else-if="view === 'voices'" />
    <DigitalHumanView v-else-if="view === 'digital-human'" />
    <SeparationView v-else-if="view === 'separation'" />
    <SlicingView v-else-if="view === 'slicing'" />
    <PolishView v-else-if="view === 'polish'" />
    <DocumentsView v-else-if="view === 'documents'" />
    <ProjectsView v-else-if="view === 'projects'" />
    <ModelsView v-else-if="view === 'models'" />
    <SettingsView v-else-if="view === 'settings'" />
  </main>
  <div v-for="t in toasts" :key="t.id" class="toast" :style="{ borderLeftColor: t.type === 'error' ? 'var(--err)' : t.type === 'success' ? 'var(--ok)' : 'var(--accent)' }">
    {{ t.msg }}
  </div>
</template>
