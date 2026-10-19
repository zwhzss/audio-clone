<script setup>
import { ref, reactive, onMounted, nextTick } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { pollJob } from '../store.js'

const tasks = ref([])
const draftFile = ref(null)
const busy = ref(false)
const job = ref(null)
const current = ref(null)
const wsDraft = ref(null)
const wsOut = ref(null)
const previewing = ref(false)
const engine = ref(null) // Web Audio 预览引擎

// 润色参数（与后端 DEFAULT_POLISH_PARAMS 一致）
const params = reactive({
  eq: { low: 0, mid_low: 0, mid: 0, mid_high: 0, high: 0 },
  highpass: 0,
  lowpass: 0,
  reverb_amount: 0,
  reverb_decay: 1.5,
  compressor: { enabled: false, threshold: -20, ratio: 3, attack: 10, release: 200, makeup: 3 },
  noise_reduction: 0,
  deesser: 0,
  loudness: 0,
})

const eqBands = [
  { key: 'low', label: '低频', freq: 120, desc: '厚度/温暖感' },
  { key: 'mid_low', label: '中低频', freq: 500, desc: '胸腔共鸣' },
  { key: 'mid', label: '中频', freq: 1500, desc: '清晰度' },
  { key: 'mid_high', label: '中高频', freq: 4000, desc: '临场感' },
  { key: 'high', label: '高频', freq: 9000, desc: '空气感' },
]

onMounted(async () => {
  tasks.value = await api.polishTasks().catch(() => [])
})

async function onUpload(e) {
  const f = e.target.files?.[0]
  if (!f) return
  draftFile.value = f
  stopPreview()
  current.value = null
  toast(`已选择初稿「${f.name}」，可用实时预览试听参数效果`)
  e.target.value = ''
}

// ---------- Web Audio 实时预览 ----------
function buildIR(ctx, seconds, decay) {
  const rate = ctx.sampleRate
  const len = Math.max(1, Math.floor(rate * seconds))
  const buf = ctx.createBuffer(2, len, rate)
  for (let ch = 0; ch < 2; ch++) {
    const data = buf.getChannelData(ch)
    for (let i = 0; i < len; i++) {
      data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay)
    }
  }
  return buf
}

async function startPreview() {
  if (!current.value?.draft) { toast('请先上传初稿音频', 'error'); return }
  stopPreview()
  engine.value = new (window.AudioContext || window.webkitAudioContext)()
  const ctx = engine.value
  const resp = await fetch(api.audioUrl(current.value.draft))
  const buf = await ctx.decodeAudioData(await resp.arrayBuffer())

  const src = ctx.createBufferSource()
  src.buffer = buf
  let node = src

  // 高通/低通
  const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = Math.max(20, params.highpass)
  const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = params.lowpass || 20000
  node.connect(hp); node = hp
  node.connect(lp); node = lp

  // 5 段 EQ
  for (const band of eqBands) {
    const f = ctx.createBiquadFilter()
    if (band.key === 'low') { f.type = 'lowshelf'; f.frequency.value = band.freq }
    else if (band.key === 'high') { f.type = 'highshelf'; f.frequency.value = band.freq }
    else { f.type = 'peaking'; f.frequency.value = band.freq; f.Q.value = 1.0 }
    f.gain.value = params.eq[band.key]
    node.connect(f); node = f
  }

  // 压缩
  const comp = ctx.createDynamicsCompressor()
  comp.threshold.value = params.compressor.enabled ? params.compressor.threshold : 0
  comp.ratio.value = params.compressor.enabled ? params.compressor.ratio : 1
  comp.attack.value = params.compressor.attack / 1000
  comp.release.value = params.compressor.release / 1000
  comp.makeupGain.value = params.compressor.enabled ? params.compressor.makeup : 0
  node.connect(comp); node = comp

  // 混响
  if (params.reverb_amount > 0) {
    const wet = ctx.createGain(); wet.gain.value = params.reverb_amount / 100
    const dry = ctx.createGain(); dry.gain.value = 1
    const conv = ctx.createConvolver()
    conv.buffer = buildIR(ctx, Math.max(0.3, params.reverb_decay), 2.5)
    node.connect(dry)
    node.connect(conv); conv.connect(wet)
    dry.connect(ctx.destination)
    wet.connect(ctx.destination)
    src.onended = () => { previewing.value = false }
  } else {
    node.connect(ctx.destination)
    src.onended = () => { previewing.value = false }
  }
  src.start()
  previewing.value = true
}

function stopPreview() {
  if (engine.value) { engine.value.close().catch(() => {}); engine.value = null }
  previewing.value = false
}

// ---------- 渲染 ----------
async function renderPolish() {
  if (!current.value?.task_dir) { toast('请先上传初稿', 'error'); return }
  busy.value = true
  try {
    const r = await api.renderPolish(current.value.task_dir, { params: JSON.parse(JSON.stringify(params)), title: current.value.name })
    job.value = { progress: 0, message: '排队中…' }
    pollJob(r.job_id, async (j) => {
      job.value = { progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        busy.value = false
        toast('润色完成！')
        current.value.polished = j.result.path
        tasks.value = await api.polishTasks()
        await nextTick()
        renderWaves()
      } else if (j.status === 'error') {
        busy.value = false
        errToast(`渲染失败：${j.message}`)
      }
    })
  } catch (e) { busy.value = false; errToast(e) }
}

async function uploadAndLoad() {
  if (!draftFile.value) return
  try {
    const up = await api.uploadPolish(draftFile.value)
    current.value = { task_dir: up.task_dir, name: up.name, draft: `polish/${up.task_dir}/draft${draftFile.value.name.slice(draftFile.value.name.lastIndexOf('.'))}`, duration: up.duration, polished: '' }
    tasks.value = await api.polishTasks()
    await nextTick()
    renderWaves()
    toast('初稿已上传，拖动滑块实时试听')
  } catch (e) { errToast(e) }
}

function loadTask(t) {
  stopPreview()
  current.value = { task_dir: t.task_dir, name: t.name, draft: t.draft, polished: t.polished, duration: t.duration }
  nextTick(() => renderWaves())
}

function renderWaves() {
  for (const [id, color, url] of [
    ['wave-d', '#9d6cff', current.value?.draft],
    ['wave-o', '#3ecf8e', current.value?.polished],
  ]) {
    const el = document.getElementById(id)
    if (!el || !url) continue
    if (id === 'wave-d' && wsDraft.value) { wsDraft.value.destroy(); wsDraft.value = null }
    if (id === 'wave-o' && wsOut.value) { wsOut.value.destroy(); wsOut.value = null }
    const ws = WaveSurfer.create({ container: el, waveColor: color + '88', progressColor: color, height: 56, url: api.audioUrl(url), barWidth: 2, barGap: 1 })
    if (id === 'wave-d') wsDraft.value = ws
    else wsOut.value = ws
  }
}

function fmtDur(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}
</script>

<template>
  <div>
    <div class="page-title">🎚️ 音频润色</div>
    <div class="page-sub">
      上传自己朗诵的初稿音频，通过 <b>EQ / 混响 / 压缩 / 降噪</b> 等专业声学手段二次加工，
      提升声音的<b>饱满度与质感</b>。支持实时试听（浏览器内即时处理）与高精度渲染导出。
    </div>

    <div class="card">
      <h3>① 上传初稿</h3>
      <div class="row">
        <label class="btn primary" style="cursor:pointer;">
          🎙️ 选择朗诵录音（mp3/wav…）
          <input type="file" accept="audio/*" style="display:none" @change="onUpload" />
        </label>
        <span v-if="draftFile" class="badge ok">{{ draftFile.name }}</span>
        <button class="btn success" :disabled="!draftFile" @click="uploadAndLoad">📤 上传</button>
      </div>
    </div>

    <template v-if="current">
      <div class="card">
        <h3>② 声学参数微调 <span class="muted" style="font-size:12px; font-weight:400;">（滑块实时生效，浏览器即时试听）</span></h3>

        <div style="margin-bottom:6px;"><b style="font-size:13px;">📊 均衡器 EQ</b><span class="muted" style="font-size:11px;"> 低频暖厚 / 中频清晰 / 高频空气感</span></div>
        <div class="slider-grid">
          <div v-for="band in eqBands" :key="band.key" class="slider-item">
            <label>{{ band.label }}（{{ band.freq }}Hz）<b>{{ params.eq[band.key] > 0 ? '+' : '' }}{{ params.eq[band.key] }} dB</b></label>
            <input type="range" min="-12" max="12" step="1" v-model.number="params.eq[band.key]" />
            <span class="muted" style="font-size:10px;">{{ band.desc }}</span>
          </div>
        </div>

        <hr />
        <div style="margin-bottom:6px;"><b style="font-size:13px;">🔧 动态与空间</b></div>
        <div class="slider-grid">
          <div class="slider-item">
            <label>高通滤波（清低频噪声）<b>{{ params.highpass > 0 ? params.highpass + ' Hz' : '关' }}</b></label>
            <input type="range" min="0" max="500" step="10" v-model.number="params.highpass" />
          </div>
          <div class="slider-item">
            <label>低通滤波（清高频噪声）<b>{{ params.lowpass > 0 ? params.lowpass + ' Hz' : '关' }}</b></label>
            <input type="range" min="0" max="20000" step="500" v-model.number="params.lowpass" />
          </div>
          <div class="slider-item">
            <label>混响量 <b>{{ params.reverb_amount }}%</b></label>
            <input type="range" min="0" max="100" step="1" v-model.number="params.reverb_amount" />
          </div>
          <div class="slider-item">
            <label>混响时长 <b>{{ params.reverb_decay.toFixed(1) }}s</b></label>
            <input type="range" min="0.1" max="5" step="0.1" v-model.number="params.reverb_decay" />
          </div>
          <div class="slider-item">
            <label>降噪强度 <b>{{ params.noise_reduction }}%</b> <span class="muted" style="font-weight:400;">（仅渲染）</span></label>
            <input type="range" min="0" max="100" step="1" v-model.number="params.noise_reduction" />
          </div>
          <div class="slider-item">
            <label>去齿音 <b>{{ params.deesser }}%</b> <span class="muted" style="font-weight:400;">（仅渲染）</span></label>
            <input type="range" min="0" max="100" step="1" v-model.number="params.deesser" />
          </div>
          <div class="slider-item">
            <label>响度标准化 <b>{{ params.loudness < 0 ? params.loudness + ' LUFS' : '关' }}</b> <span class="muted" style="font-weight:400;">（仅渲染）</span></label>
            <input type="range" min="-23" max="0" step="1" v-model.number="params.loudness" />
          </div>
          <div class="slider-item" style="grid-column: span 2;">
            <label style="justify-content:flex-start; gap:8px; cursor:pointer;">
              <input type="checkbox" v-model="params.compressor.enabled" style="accent-color:var(--accent);" />
              <b>压缩器（提升饱满度与稳定性）</b>
            </label>
            <div v-if="params.compressor.enabled" class="slider-grid" style="margin-top:8px;">
              <div class="slider-item"><label>阈值 <b>{{ params.compressor.threshold }} dB</b></label><input type="range" min="-60" max="0" step="1" v-model.number="params.compressor.threshold" /></div>
              <div class="slider-item"><label>比率 <b>{{ params.compressor.ratio }}:1</b></label><input type="range" min="1" max="20" step="0.5" v-model.number="params.compressor.ratio" /></div>
              <div class="slider-item"><label>启动 <b>{{ params.compressor.attack }} ms</b></label><input type="range" min="1" max="50" step="1" v-model.number="params.compressor.attack" /></div>
              <div class="slider-item"><label>释放 <b>{{ params.compressor.release }} ms</b></label><input type="range" min="50" max="500" step="10" v-model.number="params.compressor.release" /></div>
              <div class="slider-item"><label>增益补偿 <b>{{ params.compressor.makeup }} dB</b></label><input type="range" min="0" max="12" step="0.5" v-model.number="params.compressor.makeup" /></div>
            </div>
          </div>
        </div>

        <div class="row" style="margin-top:14px;">
          <button class="btn primary" @click="startPreview">{{ previewing ? '⏹ 停止试听' : '▶ 实时试听（带效果）' }}</button>
          <button v-if="previewing" class="btn" @click="stopPreview">停止</button>
          <button class="btn success" :disabled="busy" @click="renderPolish">🎬 渲染成品</button>
          <span class="muted" style="margin-left:auto;">💡 降噪/去齿音/响度仅渲染时生效；EQ/混响/压缩实时试听</span>
        </div>
        <template v-if="job">
          <div class="progress"><div :style="{ width: (job.progress || 0) + '%' }"></div></div>
          <div class="job-line">⚙️ {{ job.message }}</div>
        </template>
      </div>

      <div class="card">
        <h3>③ 对比试听 <span class="muted" style="font-size:12px; font-weight:400;">时长 {{ fmtDur(current.duration) }}</span></h3>
        <div style="margin-bottom:12px;">
          <div class="muted" style="font-size:12px; margin-bottom:6px;">🎙️ 原声（初稿）：</div>
          <div id="wave-d" class="wave-wrap"></div>
          <audio :src="api.audioUrl(current.draft)" controls style="width:100%; margin-top:6px; height:34px;"></audio>
        </div>
        <div>
          <div class="muted" style="font-size:12px; margin-bottom:6px;">✨ 润色后：</div>
          <div id="wave-o" class="wave-wrap"></div>
          <div v-if="current.polished" class="row" style="margin-top:6px;">
            <audio :src="api.audioUrl(current.polished)" controls style="flex:1; height:34px;"></audio>
            <a class="btn small" :href="api.audioUrl(current.polished)" download>⬇ 下载润色成品</a>
          </div>
          <div v-else class="empty" style="padding:14px;">尚未渲染，调整参数后点「渲染成品」。</div>
        </div>
      </div>
    </template>

    <div class="card">
      <h3>📚 历史润色记录</h3>
      <div v-if="!tasks.length" class="empty">还没有润色记录。</div>
      <div v-for="t in tasks" :key="t.task_dir" class="list-item" style="padding:10px 14px;">
        <div style="flex:1; min-width:0;">
          <div class="row" style="gap:8px;">
            <span class="name">{{ t.name }}</span>
            <span class="badge" :class="t.has_result ? 'ok' : ''">{{ t.has_result ? '已润色' : '未渲染' }}</span>
            <span class="muted" style="font-size:11px;">{{ fmtDur(t.duration) }}</span>
          </div>
        </div>
        <button class="btn small" @click="loadTask(t)">打开</button>
      </div>
    </div>
  </div>
</template>
