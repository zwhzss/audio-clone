<script setup>
import { ref, reactive, onMounted, nextTick } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { pollJob } from '../store.js'

const engines = ref([])
const tasks = ref([])
const page = ref(1)
const totalPages = ref(1)
const total = ref(0)
const searchQuery = ref('')
const favOnly = ref(false)
const engineId = ref('uvr5')
const modelId = ref('')
const songFile = ref(null)
const busy = ref(false)
const job = ref(null)
const current = ref(null)
const wsVocal = ref(null)
const wsAcc = ref(null)

onMounted(async () => {
  engines.value = await api.sepEngines()
  if (engines.value.length) {
    engineId.value = engines.value[0].id
    modelId.value = engines.value[0].models[0].id
  }
  await loadTasks()
})

async function loadTasks(p) {
  page.value = p || 1
  try {
    const r = await api.sepTasks(page.value, 10, searchQuery.value)
    tasks.value = (r.items || []).filter((t) => !favOnly.value || t.favorite)
    totalPages.value = r.pages || 1
    total.value = r.total || 0
  } catch { tasks.value = [] }
}

function onSearch() {
  loadTasks(1)
}

function toggleFavFilter() {
  favOnly.value = !favOnly.value
  loadTasks(1)
}

const curModels = () => engines.value.find((e) => e.id === engineId.value)?.models || []

function onEngineChange() {
  const ms = curModels()
  if (ms.length) modelId.value = ms[0].id
}

async function onUpload(e) {
  const f = e.target.files?.[0]
  if (!f) return
  songFile.value = f
  toast(`已选择「${f.name}」`)
  e.target.value = ''
}

async function runSep() {
  if (!songFile.value) { toast('请先选择歌曲文件', 'error'); return }
  busy.value = true
  try {
    const up = await api.uploadSepSong(songFile.value)
    const r = await api.runSepTask(up.task_dir, { engine: engineId.value, model: modelId.value, title: songFile.value.name })
    job.value = { progress: 0, message: '排队中…' }
    pollJob(r.job_id, async (j) => {
      job.value = { progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        busy.value = false
        toast('分离完成！人声（词）与伴奏（曲）已生成')
        current.value = { task_dir: up.task_dir, song: j.result.song, vocals: j.result.vocals, accompaniment: j.result.accompaniment, duration: j.result.duration }
        await loadTasks()
        await nextTick()
        renderWaves()
      } else if (j.status === 'error') {
        busy.value = false
        errToast(`分离失败：${j.message}`)
      }
    })
  } catch (e) { busy.value = false; errToast(e) }
}

function loadTask(t) {
  if (!t.has_result) return
  current.value = { task_dir: t.task_dir, song: t.song, vocals: `separation/${t.task_dir}/vocals.wav`, accompaniment: `separation/${t.task_dir}/accompaniment.wav`, duration: t.duration }
  nextTick(() => renderWaves())
}

function renderWaves() {
  for (const [id, color, url] of [
    ['wave-v', '#f0a35e', current.value?.vocals],
    ['wave-a', '#5ec8f0', current.value?.accompaniment],
  ]) {
    const el = document.getElementById(id)
    if (!el || !url) continue
    if (id === 'wave-v' && wsVocal.value) { wsVocal.value.destroy(); wsVocal.value = null }
    if (id === 'wave-a' && wsAcc.value) { wsAcc.value.destroy(); wsAcc.value = null }
    const ws = WaveSurfer.create({ container: el, waveColor: color + '88', progressColor: color, height: 64, url: api.audioUrl(url), barWidth: 2, barGap: 1 })
    if (id === 'wave-v') wsVocal.value = ws
    else wsAcc.value = ws
  }
}

function fmtDur(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

async function deleteTask(t) {
  const name = t.original_name || t.song.split('/').pop()
  if (!confirm(`确定删除「${name}」？原歌曲与分离结果将一并移除。`)) return
  try {
    const r = await api.deleteSepTask(t.task_dir)
    if (current.value?.task_dir === t.task_dir) current.value = null
    await loadTasks(page.value)
    toast(`已删除「${name}」，释放 ${r.freed_mb} MB`)
  } catch (e) { errToast(e) }
}

async function toggleFavorite(t) {
  try {
    const r = await api.toggleSepFavorite(t.task_dir)
    t.favorite = r.favorite
    toast(r.favorite ? '已标记为喜欢 ❤️' : '已取消喜欢')
  } catch (e) { errToast(e) }
}
</script>

<template>
  <div>
    <div class="page-title">🎵 歌曲词曲分离</div>
    <div class="page-sub">
      上传歌曲，一键分离出 <b>人声（词）</b> 与 <b>伴奏（曲）</b>。全部本地免费处理（UVR5 / Demucs 开源模型），
      无需训练数据。分离后的人声轨还可作为声音克隆素材，或用于制作「翻唱/朗读」作品。
    </div>

    <div class="card">
      <h3>① 选择引擎与上传歌曲</h3>
      <div class="row">
        <select v-model="engineId" @change="onEngineChange" style="flex:1;">
          <option v-for="e in engines" :key="e.id" :value="e.id">{{ e.name }}（{{ e.cloud ? '云' : '本地免费' }}）</option>
        </select>
        <select v-model="modelId" style="flex:2;">
          <option v-for="m in curModels()" :key="m.id" :value="m.id">{{ m.name }} — {{ m.desc }}</option>
        </select>
      </div>
      <div class="hint" style="margin-top:8px;">
        💡 UVR5：GPT-SoVITS 内置模型，中文歌效果好；HP2 通用（快）、BS-Roformer 最干净（慢）。
        Demucs：Meta 开源 SOTA 模型，首次使用自动下载模型。分离耗时约 1~5 分钟。
      </div>
      <div class="row" style="margin-top:10px;">
        <label class="btn primary" style="cursor:pointer;">
          📁 选择歌曲（mp3/wav/flac…）
          <input type="file" accept="audio/*" style="display:none" @change="onUpload" />
        </label>
        <span v-if="songFile" class="badge ok">{{ songFile.name }}</span>
        <button class="btn success" :disabled="busy || !songFile" @click="runSep">🎬 开始分离</button>
      </div>
      <template v-if="job">
        <div class="progress"><div :style="{ width: (job.progress || 0) + '%' }"></div></div>
        <div class="job-line">⚙️ {{ job.message }}</div>
      </template>
    </div>

    <div v-if="current" class="card">
      <h3>② 分离结果（词 = 人声轨 / 曲 = 伴奏轨）
        <span class="muted" style="font-size:12px; font-weight:400;">时长 {{ fmtDur(current.duration) }}</span>
      </h3>
      <div style="margin-bottom:14px;">
        <div class="muted" style="font-size:12px; margin-bottom:6px;">🎤 人声（歌词）：</div>
        <div id="wave-v" class="wave-wrap"></div>
        <div class="row" style="margin-top:6px;">
          <audio :src="api.audioUrl(current.vocals)" controls style="flex:1; height:34px;"></audio>
          <a class="btn small" :href="api.audioUrl(current.vocals)" download>⬇ 下载人声</a>
        </div>
      </div>
      <div>
        <div class="muted" style="font-size:12px; margin-bottom:6px;">🎹 伴奏（曲子）：</div>
        <div id="wave-a" class="wave-wrap"></div>
        <div class="row" style="margin-top:6px;">
          <audio :src="api.audioUrl(current.accompaniment)" controls style="flex:1; height:34px;"></audio>
          <a class="btn small" :href="api.audioUrl(current.accompaniment)" download>⬇ 下载伴奏</a>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between; margin-bottom:10px;">
        <h3 style="margin:0;">📚 历史分离记录 <span class="muted" style="font-size:12px; font-weight:400;">（共 {{ total }} 首）</span></h3>
        <div class="row" style="gap:8px;">
          <button class="btn small" :class="favOnly ? 'primary' : ''" @click="toggleFavFilter">
            {{ favOnly ? '❤️ 只看喜欢' : '🤍 全部' }}
          </button>
          <input v-model="searchQuery" type="text" placeholder="🔍 输入歌曲名模糊搜索…" style="width:220px;" @keyup.enter="onSearch" @input="onSearch" />
        </div>
      </div>
      <div v-if="!tasks.length" class="empty">还没有分离记录。</div>
      <div v-for="t in tasks" :key="t.task_dir" class="list-item" style="padding:10px 14px;">
        <div style="flex:1; min-width:0;">
          <div class="row" style="gap:8px;">
            <span class="name">{{ t.original_name || t.song.split('/').pop() }}</span>
            <span v-if="t.favorite" class="badge ok" style="font-size:14px;">❤️</span>
            <span class="badge" :class="t.has_result ? 'ok' : ''">{{ t.has_result ? '已分离' : '待分离' }}</span>
            <span class="muted" style="font-size:11px;">{{ fmtDur(t.duration) }}</span>
          </div>
        </div>
        <button class="btn small" @click="toggleFavorite(t)" :title="t.favorite ? '取消喜欢' : '标记为喜欢'">
          {{ t.favorite ? '❤️' : '🤍' }}
        </button>
        <button class="btn small" :disabled="!t.has_result" @click="loadTask(t)">查看结果</button>
        <button class="btn small danger" @click="deleteTask(t)">删除</button>
      </div>
      <!-- 分页控件 -->
      <div v-if="totalPages > 1" class="row" style="justify-content:center; gap:6px; margin-top:14px;">
        <button class="btn small" :disabled="page <= 1" @click="loadTasks(page - 1)">← 上一页</button>
        <span class="muted" style="font-size:12px;">{{ page }} / {{ totalPages }}</span>
        <button class="btn small" :disabled="page >= totalPages" @click="loadTasks(page + 1)">下一页 →</button>
      </div>
    </div>
  </div>
</template>
