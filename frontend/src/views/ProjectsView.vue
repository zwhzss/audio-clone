<script setup>
import { ref } from 'vue'
import { store, refreshAll, pollJob } from '../store.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'

const busy = ref({})
const playing = ref(null) // 正在播放的项目

const statusMap = {
  draft: '未生成', generating: '生成中', voices_ready: '声音轨就绪',
  rendering: '渲染中', ready: '成品就绪', error: '失败',
}
const toneMap = {
  voices_ready: 'ok', ready: 'ok', generating: 'warn', rendering: 'warn', error: 'err', draft: '',
}

async function generate(p) {
  busy.value[p.id] = '生成中'
  try {
    const r = await api.generateProject(p.id)
    pollJob(r.job_id, async (j) => {
      if (j.status === 'success' || j.status === 'error') {
        busy.value[p.id] = ''
        j.status === 'success' ? toast(`「${p.title}」声音轨生成完成`) : errToast(`生成失败：${j.message}`)
        await refreshAll()
      }
    })
  } catch (e) { busy.value[p.id] = ''; errToast(e) }
}

async function render(p) {
  busy.value[p.id] = '渲染中'
  try {
    const r = await api.renderProject(p.id)
    pollJob(r.job_id, async (j) => {
      if (j.status === 'success' || j.status === 'error') {
        busy.value[p.id] = ''
        j.status === 'success' ? toast(`「${p.title}」成品渲染完成`) : errToast(`渲染失败：${j.message}`)
        await refreshAll()
      }
    })
  } catch (e) { busy.value[p.id] = ''; errToast(e) }
}

async function remove(p) {
  if (!confirm(`确定删除作品「${p.title}」？所有音频文件将一并删除。`)) return
  try {
    await api.deleteProject(p.id)
    await refreshAll()
    toast('已删除')
  } catch (e) { errToast(e) }
}

function openStudio(p) {
  localStorage.setItem('ac_view', 'studio')
  localStorage.setItem('ac_project', p.id)
  location.reload()
}
</script>

<template>
  <div>
    <div class="page-title">📚 有声读物作品</div>
    <div class="page-sub">所有作品（声音模型、分段音频、成品）都存储在本地磁盘，可随时回到工作台继续微调。</div>

    <div v-if="!store.projects.length" class="empty">还没有作品。到「合成工作台」创建第一个有声读物项目。</div>

    <div v-for="p in store.projects" :key="p.id" class="list-item" style="align-items:flex-start;">
      <div style="flex:1; min-width:0;">
        <div class="row" style="gap:8px;">
          <span class="name">{{ p.title }}</span>
          <span class="badge" :class="toneMap[p.status]">{{ statusMap[p.status] || p.status }}</span>
          <span v-if="p.cost > 0" class="badge accent">云费用 ¥{{ p.cost.toFixed(4) }}</span>
        </div>
        <div class="sub" style="margin-top:6px;">
          文稿：{{ p.document_title || '（未关联）' }} · 声音：{{ p.voice_name || '（未选择）' }}
          <template v-if="p.duration"> · 成品时长 {{ Math.floor(p.duration / 60) }}分{{ Math.round(p.duration % 60) }}秒</template>
          · {{ p.segment_count }} 段
          <span v-if="busy[p.id]" class="badge warn" style="margin-left:6px;">{{ busy[p.id] }}</span>
        </div>
      </div>
      <div class="actions">
        <button class="btn small primary" @click="openStudio(p)">打开工作台</button>
        <button class="btn small" :disabled="!!busy[p.id]" @click="generate(p)">重新生成</button>
        <button class="btn small success" :disabled="!!busy[p.id] || (p.status !== 'voices_ready' && p.status !== 'ready')" @click="render(p)">渲染成品</button>
        <button class="btn small" :disabled="!p.final_path" @click="playing = p">播放</button>
        <button class="btn small danger" @click="remove(p)">删除</button>
      </div>
    </div>

    <!-- 播放弹窗 -->
    <div v-if="playing" class="modal-mask" @click.self="playing = null">
      <div class="modal">
        <h3>▶ {{ playing.title }} — 最终成品</h3>
        <audio :src="api.audioUrl(playing.final_path)" controls autoplay style="margin-bottom:12px;"></audio>
        <div class="row" style="justify-content:flex-end;">
          <a class="btn primary" :href="api.audioUrl(playing.final_path)" download>⬇ 下载成品</a>
          <button class="btn" @click="playing = null">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
