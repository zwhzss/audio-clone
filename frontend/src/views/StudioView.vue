<script setup>
import { ref, reactive, computed, watch, onBeforeUnmount, nextTick } from 'vue'
import WaveSurfer from 'wavesurfer.js'
import { store, refreshAll, pollJob } from '../store.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { PreviewEngine } from '../audioPreview.js'

// ---------- 状态 ----------
const projectId = ref('')
const detail = ref(null)
const params = reactive({
  speed: 1.0, pitch: 0, volume: 1.0,
  eq: { low: 0, mid: 0, high: 0 },
  fade_in: 0, fade_out: 0, gap: 0.6,
  bgm_volume: 0.25, ducking: 0, ducking_mode: 'off',
  ducking_curve: [
    { pct: 0, level: 0.3 }, { pct: 25, level: 0.3 }, { pct: 50, level: 0.3 },
    { pct: 75, level: 0.3 }, { pct: 100, level: 0.3 },
  ],
})
const genJob = ref(null)
const renderJob = ref(null)
const genBusy = ref(false)
const renderBusy = ref(false)
const preview = ref(null) // PreviewEngine 实例
const previewing = ref(false)
const previewTime = ref('0:00')
const wsWave = ref(null)
const wsFinal = ref(null)

// 分段级参数
const expandedSeg = ref(null)
const segDraft = reactive({ speed: 1.0, pitch: 0, volume: 1.0, eq: { low: 0, mid: 0, high: 0 } })
const segPreviewing = ref(-1) // 正在试听的分段 idx，-1 表示未试听
const segPreviewEngine = ref(null)
// 分段文字编辑 / 重切
const editingSeg = ref(-1)
const editDraft = ref('')
const splitBusy = ref(false)
const resynthBusy = ref(-1)
const segJob = ref(null)
const checkpoints = ref([])
const curCheckpoint = ref('')   // 当前选中的 checkpoint（v-model 驱动，避免 :selected 静态绑定不刷新）
const currentHumanId = ref('')

// 章节
const chapters = ref([])

// 对比试听
const compareSel = ref([])
const compareText = ref('')
const compareJob = ref(null)
const compareBusy = ref(false)
const compareItems = ref([])

const selVoiceId = ref('')
const selDocId = ref('')
const newTitle = ref('')
const digitalHumans = ref([])
const refAudios = ref([])
const selRefAudio = ref('')
const selAuxRefs = ref([]) // 辅助参考多选（idx 列表，用于混合音色）
const humanDataDir = ref('')
const refSrcDirs = ref({}) // 各数据源目录 { main, s1, s2 }（参考音频路径按源解析）
const selectedBaseModel = ref('')

// ---------- 初始化 ----------
const defaultsLoaded = api.mixDefaults().then((d) => {
  Object.keys(d).forEach((k) => {
    if (k === 'eq') params.eq = { ...d.eq }
    else if (k === 'ducking_curve') params.ducking_curve = JSON.parse(JSON.stringify(d.ducking_curve || params.ducking_curve))
    else if (k in params) params[k] = d[k]
  })
})

// 从「作品」页跳转时恢复选中项目
const savedProject = localStorage.getItem('ac_project')
if (savedProject) {
  projectId.value = savedProject
  localStorage.removeItem('ac_project')
}

watch(projectId, async (id) => {
  if (!id) { detail.value = null; return }
  await loadDetail(id)
  loadVerifyMode()
}, { immediate: true })

// ---------- 合成后文字校验模式（全局设置） ----------
const VERIFY_LABEL = { full: '全部句子', smart: '仅短句与对话', off: '关闭' }
const verifyMode = ref('')
async function loadVerifyMode() {
  if (verifyMode.value) return
  try {
    const s = await api.getSettings()
    verifyMode.value = s.asr_verify_mode || 'smart'
  } catch (e) { verifyMode.value = 'smart' }
}
async function saveVerifyMode() {
  try {
    await api.saveSettings({ asr_verify_mode: verifyMode.value })
    toast(`文字校验已设为「${VERIFY_LABEL[verifyMode.value] || verifyMode.value}」`)
  } catch (e) { errToast(e) }
}

async function loadDetail(id) {
  detail.value = await api.getProject(id)
  detail.value._ts = Date.now()  // 缓存破坏时间戳
  Object.keys(detail.value.params || {}).forEach((k) => {
    if (k === 'eq') params.eq = { ...detail.value.params.eq }
    else if (k === 'ducking_curve') params.ducking_curve = JSON.parse(JSON.stringify(detail.value.params.ducking_curve || params.ducking_curve))
    else if (k in params) params[k] = detail.value.params[k]
  })
  chapters.value = await api.chapters(id).catch(() => [])
  // 加载 checkpoint 列表
  if (detail.value.digital_human_id) {
    currentHumanId.value = detail.value.digital_human_id
    checkpoints.value = await api.humanCheckpoints(detail.value.digital_human_id).catch(() => [])
    // 同步下拉选中项（is_current 来自后端，切换 checkpoint 后 loadDetail 会刷新这里）
    const cur = checkpoints.value.find((c) => c.is_current)
    curCheckpoint.value = cur ? cur.name : (checkpoints.value[0]?.name || '')
  } else {
    checkpoints.value = []
    currentHumanId.value = ''
    curCheckpoint.value = ''
  }
  // 同步「修改配置」面板下拉：与项目实际绑定关系保持一致（避免下拉与上方权威展示错位）
  const dhId = detail.value.digital_human_id
  if (dhId) {
    if (selVoiceId.value !== 'h:' + dhId) selVoiceId.value = 'h:' + dhId
    await loadHumanRefs(dhId)
  } else {
    if (selVoiceId.value !== (detail.value.voice_id || '')) selVoiceId.value = detail.value.voice_id || ''
    refAudios.value = []
    selRefAudio.value = ''
    humanDataDir.value = ''
  }
  await nextTick()
  renderWaves()
}

function renderWaves() {
  if (!detail.value) return
  const ts = `?t=${Date.now()}`
  const vt = detail.value.voice_track_path
  const waveBox = document.getElementById('wave-vt')
  if (vt && waveBox) {
    if (wsWave.value) { wsWave.value.destroy(); wsWave.value = null }
    wsWave.value = WaveSurfer.create({
      container: waveBox,
      waveColor: '#6c8cff88', progressColor: '#6c8cff',
      height: 72, url: api.audioUrl(vt) + ts, barWidth: 2, barGap: 1,
    })
  }
  const fin = detail.value.final_path
  const waveBox2 = document.getElementById('wave-final')
  if (fin && waveBox2) {
    if (wsFinal.value) { wsFinal.value.destroy(); wsFinal.value = null }
    wsFinal.value = WaveSurfer.create({
      container: waveBox2,
      waveColor: '#3ecf8e88', progressColor: '#3ecf8e',
      height: 56, url: api.audioUrl(fin) + ts, barWidth: 2, barGap: 1,
    })
  }
}

// ---------- 项目创建（独立） ----------
async function quickCreate() {
  if (!newTitle.value.trim()) { toast('请输入项目名称', 'error'); return }
  try {
    const p = await api.createProject({ title: newTitle.value.trim() })
    await refreshAll()
    projectId.value = p.id
    toast('项目已创建，现在选择文稿和声音')
  } catch (e) { errToast(e) }
}

// ---------- 保存绑定（提取为公共函数） ----------
async function saveBinding() {
  if (!detail.value || !selDocId.value || !selVoiceId.value) { toast('请选择文稿和声音', 'error'); return false }
  let voiceId = selVoiceId.value
  let humanId = ''
  if (selVoiceId.value.startsWith('h:')) {
    humanId = selVoiceId.value.slice(2)
    const h = digitalHumans.value.find((d) => d.id === humanId)
    if (!h?.voice_id) { toast('该数字人未绑定声音模型', 'error'); return false }
    voiceId = h.voice_id
  }
  await api.updateProject(projectId.value, { document_id: selDocId.value, voice_id: voiceId, digital_human_id: humanId })
  await loadDetail(projectId.value)
  return true
}

async function bindContent() {
  try {
    if (await saveBinding()) toast('文稿和声音已绑定，可点击「生成声音轨」')
  } catch (e) { errToast(e) }
}

// 文稿选择变更 → 自动保存
async function onDocChange() {
  if (!projectId.value || !selDocId.value) return
  try {
    await api.updateProject(projectId.value, { document_id: selDocId.value })
    await loadDetail(projectId.value)
  } catch (e) { errToast(e) }
}

// 数字人/声音选择变更 → 自动保存
async function onVoiceChange() {
  if (!projectId.value || !selVoiceId.value) return
  let voiceId = selVoiceId.value
  let humanId = ''
  if (selVoiceId.value.startsWith('h:')) {
    humanId = selVoiceId.value.slice(2)
    const h = digitalHumans.value.find((d) => d.id === humanId)
    if (!h?.voice_id) { toast('该数字人未绑定声音模型', 'error'); return }
    voiceId = h.voice_id
  }
  try {
    await api.updateProject(projectId.value, { voice_id: voiceId, digital_human_id: humanId })
    await loadDetail(projectId.value)
  } catch (e) { errToast(e) }
}

// 切换 checkpoint
async function onCheckpointChange(e) {
  const cp = checkpoints.value.find(c => c.name === e.target.value)
  if (!cp || !currentHumanId.value) return
  try {
    await api.setCheckpoint(currentHumanId.value, { gpt_path: cp.gpt_path, s2_path: cp.s2_path })
    await loadDetail(projectId.value)
    toast(`已切换到 ${cp.name}`)
  } catch (e) { errToast(e) }
}

async function createProject() {
  try {
    if (!selDocId.value || !selVoiceId.value) { toast('请先选择文稿和声音', 'error'); return }
    let voiceId = selVoiceId.value
    let humanId = ''
    if (selVoiceId.value.startsWith('h:')) {
      humanId = selVoiceId.value.slice(2)
      const h = digitalHumans.value.find((d) => d.id === humanId)
      if (!h?.voice_id) { toast('该数字人未绑定声音模型', 'error'); return }
      voiceId = h.voice_id
    }
    const p = await api.createProject({ title: newTitle.value || '未命名作品', document_id: selDocId.value, voice_id: voiceId, digital_human_id: humanId })
    await refreshAll()
    projectId.value = p.id
    toast(humanId ? '🎭 项目已创建（数字人模式）' : '项目已创建，下一步：生成声音轨')
  } catch (e) { errToast(e) }
}

async function saveParams() {
  if (!projectId.value) return
  try {
    await api.updateProject(projectId.value, { params: { ...params } })
    if (preview.value) preview.value.setParams(params)
  } catch (e) { errToast(e) }
}

// 混音预设
const PRESETS = {
  rich: { speed: 1.0, pitch: 0, volume: 1.0, eq: { low: 4, mid: 2, high: 1 }, bgm_volume: 0.2, fade_in: 0.5, fade_out: 1.0, gap: 0.6 },
  warm: { speed: 0.95, pitch: -1, volume: 1.0, eq: { low: 3, mid: 1, high: -1 }, bgm_volume: 0.25, fade_in: 1.0, fade_out: 1.5, gap: 0.8 },
  clear: { speed: 1.05, pitch: 0, volume: 0.95, eq: { low: -1, mid: 2, high: 4 }, bgm_volume: 0.15, fade_in: 0.3, fade_out: 0.8, gap: 0.5 },
  neutral: { speed: 1.0, pitch: 0, volume: 1.0, eq: { low: 0, mid: 0, high: 0 }, bgm_volume: 0.25, fade_in: 0, fade_out: 0, gap: 0.6 },
}

function applyPreset(name) {
  const p = PRESETS[name]
  if (!p) return
  Object.assign(params, p)
  saveParams()
  toast(`已应用「${name === 'rich' ? '饱满磁性' : name === 'warm' ? '温暖柔和' : name === 'clear' ? '清晰明亮' : '默认'}」预设`)
}

// ---------- 生成声音轨 ----------
const segReady = computed(() => (detail.value?.segments || []).filter((s) => s.status === 'ready').length)
const segTodo = computed(() => (detail.value?.segments || []).length - segReady.value)

async function generate(resume = false) {
  if (!projectId.value) return
  // 自动检测未保存的文稿/声音变更并先保存（防止用旧文稿生成）
  if (selDocId.value) {
    let curVoiceId = selVoiceId.value
    if (curVoiceId.startsWith('h:')) {
      const h = digitalHumans.value.find((d) => d.id === curVoiceId.slice(2))
      curVoiceId = h?.voice_id || ''
    }
    const docChanged = detail.value?.document_id != null && selDocId.value !== detail.value.document_id
    const voiceChanged = detail.value?.voice_id != null && curVoiceId && curVoiceId !== detail.value.voice_id
    if (docChanged || voiceChanged) {
      try {
        if (!(await saveBinding())) return
        toast('已自动保存新文稿/声音配置')
      } catch (e) { errToast(e); return }
    }
  }
  genBusy.value = true
  try {
    const r = await api.generateProject(projectId.value, resume)
    genJob.value = { id: r.job_id, progress: 0, message: resume ? '断点续做，排队中…' : '排队中…' }
    pollJob(r.job_id, async (j) => {
      genJob.value = { id: r.job_id, progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        genBusy.value = false
        toast('声音轨生成完成，可开始试听与微调')
        await loadDetail(projectId.value)
        await refreshAll()
      } else if (j.status === 'error') {
        genBusy.value = false
        errToast(`生成失败：${j.message}`)
      }
    })
  } catch (e) { genBusy.value = false; errToast(e) }
}

// ---------- 实时预览 ----------
async function startPreview() {
  if (!detail.value?.voice_track_path) { toast('请先生成声音轨', 'error'); return }
  if (!preview.value) preview.value = new PreviewEngine()
  try {
    previewing.value = true
    previewTime.value = '加载中…'
    const bgm = detail.value.bgm_path ? api.audioUrl(detail.value.bgm_path) : null
    await preview.value.load(api.audioUrl(detail.value.voice_track_path), bgm)
    preview.value.onProgress = (t) => { previewTime.value = fmt(t) }
    preview.value.onEnd = () => { previewing.value = false; previewTime.value = '0:00' }
    preview.value.play(params)
  } catch (e) {
    previewing.value = false
    errToast(`预览失败：${e.message}`)
  }
}

// ---------- 数字人选择 ----------
async function fetchDigitalHumans() {
  digitalHumans.value = await api.humans().catch(() => [])
}

// 加载数字人列表
fetchDigitalHumans()

// ---------- 参考音频 ----------
async function loadRefAudios(humanId) {
  // 合并主/s1/s2 三个数据源的音频（s1/s2 分离训练的数字人主数据可能为空）
  const all = []
  for (const src of ['main', 's1', 's2']) {
    try {
      const data = await api.humanDataList(humanId, 1, 500, src)
      const items = (data.items || []).filter((item) => item.text && item.text.trim())
      items.forEach((it) => all.push({ ...it, src, label: src === 'main' ? '' : (src === 's1' ? '🗣s1' : '🎤s2') }))
    } catch (e) { /* ignore */ }
  }
  refAudios.value = all
}

// 按当前项目绑定的参考音频（ref_audio_name=音频文件名）回显下拉选中项，
// 找不到则回退「自动选择」——保证下拉与上方权威展示（voice.engine_config）一致
function syncSelRefAudio() {
  const name = detail.value?.ref_audio_name
  if (!name || !refAudios.value.length) { selRefAudio.value = ''; return }
  const idx = refAudios.value.findIndex((r) => r.audio === name)
  selRefAudio.value = idx >= 0 ? idx : ''
}

// 加载数字人的 data_dir + 参考音频列表，并回显主参考/辅助参考选中项
async function loadHumanRefs(humanId) {
  let voiceId = ''
  try {
    const h = await api.human(humanId)
    humanDataDir.value = h.data_dir || ''
    refSrcDirs.value = { main: h.data_dir || '', s1: h.s1_data_dir || '', s2: h.s2_data_dir || '' }
    voiceId = h.voice_id || ''
  } catch (e) { /* ignore */ }
  await loadRefAudios(humanId)
  syncSelRefAudio()
  // 回显辅助参考（需 refAudios 已加载）
  const v = store.voices.find((x) => x.id === voiceId)
  const savedAux = v?.engine_config?.aux_ref_paths || []
  selAuxRefs.value = savedAux.length
    ? refAudios.value.map((r, i) => {
        const dir = refSrcDirs.value[r.src || 'main'] || humanDataDir.value
        return savedAux.includes(dir + '/' + r.audio) ? i : -1
      }).filter((i) => i >= 0)
    : []
}

// 辅助参考勾选变化：保存 aux_ref_paths 到数字人绑定的声音（merge 更新，不影响其它配置）
watch(selAuxRefs, async (val) => {
  if (!selVoiceId.value?.startsWith('h:')) return
  const humanId = selVoiceId.value.slice(2)
  const h = digitalHumans.value.find((d) => d.id === humanId)
  const voiceId = h?.voice_id
  if (!voiceId || !refAudios.value.length) return
  const mainIdx = Number(selRefAudio.value)
  const paths = val
    .filter((i) => i !== mainIdx) // 排除主参考自身
    .map((i) => {
      const dir = refSrcDirs.value[refAudios.value[i].src || 'main'] || humanDataDir.value
      return dir + '/' + refAudios.value[i].audio
    })
  try {
    await api.updateVoice(voiceId, { engine_config: { aux_ref_paths: paths } })
    if (paths.length) toast(`已保存 ${paths.length} 个辅助参考（混合音色将在合成时融合）`)
  } catch (e) { errToast(e) }
})

function refAudioUrl(idx) {
  const ref = refAudios.value[idx]
  if (!ref) return ''
  const humanId = selVoiceId.value.startsWith('h:') ? selVoiceId.value.slice(2) : ''
  if (!humanId) return ''
  return `/api/humans/${humanId}/audio/${ref.audio}?source=${ref.src || 'main'}`
}

async function onRefAudioChange() {
  if (selRefAudio.value === '' || !refAudios.value[selRefAudio.value]) return
  const ref = refAudios.value[selRefAudio.value]
  const humanId = selVoiceId.value.startsWith('h:') ? selVoiceId.value.slice(2) : ''
  try {
    const srcDir = refSrcDirs.value[ref.src || 'main'] || humanDataDir.value
    const refPath = srcDir + '/' + ref.audio
    // 保存参考音频到 Project
    if (projectId.value) {
      await api.updateProject(projectId.value, { ref_audio_path: refPath, ref_text: ref.text })
    }
    // 同步更新声音库的 engine_config（兼容旧逻辑）
    if (humanId) {
      const h = digitalHumans.value.find((d) => d.id === humanId)
      const voiceId = h?.voice_id
      if (voiceId) {
        await api.updateVoice(voiceId, {
          engine_config: {
            ref_path: refPath,
            ref_text: ref.text,
            ref_lang: 'zh',
          }
        })
      }
    }
    toast('参考音频已设置')
    // 刷新项目详情（ref_audio_name / ref_text_preview 跟随显示）
    if (projectId.value) await loadDetail(projectId.value)
  } catch (e) { errToast(e) }
}

// 监听声音选择变化，加载参考音频并回显当前选中项
watch(selVoiceId, async (newVal) => {
  if (newVal && newVal.startsWith('h:')) {
    await loadHumanRefs(newVal.slice(2))
  } else {
    refAudios.value = []
    selRefAudio.value = ''
    humanDataDir.value = ''
  }
})

function stopPreview() {
  if (preview.value) preview.value.stop()
  previewing.value = false
  previewTime.value = '0:00'
}

function fmt(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// ---------- 渲染成品 ----------
async function render() {
  if (!projectId.value) return
  renderBusy.value = true
  try {
    await api.updateProject(projectId.value, { params: { ...params } })
    const r = await api.renderProject(projectId.value)
    renderJob.value = { id: r.job_id, progress: 0, message: '排队中…' }
    pollJob(r.job_id, async (j) => {
      renderJob.value = { id: r.job_id, progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        renderBusy.value = false
        toast('成品渲染完成！')
        await loadDetail(projectId.value)
        await refreshAll()
      } else if (j.status === 'error') {
        renderBusy.value = false
        errToast(`渲染失败：${j.message}`)
      }
    })
  } catch (e) { renderBusy.value = false; errToast(e) }
}

// ---------- 背景乐上传 ----------
async function onBgm(e) {
  const f = e.target.files?.[0]
  if (!f || !projectId.value) return
  try {
    await api.uploadBgm(projectId.value, f)
    await loadDetail(projectId.value)
    toast('背景音乐已上传')
  } catch (err) { errToast(err) }
}

// ---------- 分段级参数 ----------
function openSegParams(seg) {
  if (expandedSeg.value === seg.idx) { expandedSeg.value = null; return }
  expandedSeg.value = seg.idx
  const p = seg.params || {}
  segDraft.speed = p.speed ?? 1.0
  segDraft.pitch = p.pitch ?? 0
  segDraft.volume = p.volume ?? 1.0
  segDraft.eq = { low: p.eq?.low ?? 0, mid: p.eq?.mid ?? 0, high: p.eq?.high ?? 0 }
}

async function applySegParams(seg) {
  try {
    const clean = {}
    if (Math.abs(segDraft.speed - 1) > 0.01) clean.speed = segDraft.speed
    if (Math.abs(segDraft.pitch) > 0.01) clean.pitch = segDraft.pitch
    if (Math.abs(segDraft.volume - 1) > 0.01) clean.volume = segDraft.volume
    const eq = {}
    if (Math.abs(segDraft.eq.low) > 0.01) eq.low = segDraft.eq.low
    if (Math.abs(segDraft.eq.mid) > 0.01) eq.mid = segDraft.eq.mid
    if (Math.abs(segDraft.eq.high) > 0.01) eq.high = segDraft.eq.high
    if (Object.keys(eq).length) clean.eq = eq
    await api.setSegmentParams(projectId.value, seg.idx, clean)
    seg.params = clean
    toast(`分段 #${seg.idx + 1} 参数已保存，渲染时生效`)
  } catch (e) { errToast(e) }
}

async function clearSegParams(seg) {
  try {
    await api.clearSegmentParams(projectId.value, seg.idx)
    seg.params = {}
    toast(`分段 #${seg.idx + 1} 已恢复全局参数`)
  } catch (e) { errToast(e) }
}

const hasSegParams = (seg) => seg.params && Object.keys(seg.params).length > 0

// ---------- 合成前修改/删除分片 ----------
function editSegText(seg) {
  if (editingSeg.value === seg.idx) { editingSeg.value = -1; return }
  editingSeg.value = seg.idx
  editDraft.value = seg.text
}

async function saveSegText(seg) {
  const t = editDraft.value.trim()
  if (!t) { toast('文字不能为空；要让这一段不合成请点「删除」', 'error'); return }
  try {
    await api.setSegmentText(projectId.value, seg.idx, t)
    seg.text = t
    editingSeg.value = -1
    toast(`分段 #${seg.idx + 1} 已保存，下次生成声音轨时用这段文字`)
  } catch (e) { errToast(e) }
}

async function delSeg(seg) {
  const preview = seg.text.slice(0, 40) + (seg.text.length > 40 ? '…' : '')
  if (!confirm(`删除分段 #${seg.idx + 1}？\n\n「${preview}」\n\n这一段将不再合成，后面分段序号自动前移。`)) return
  try {
    await api.deleteSegment(projectId.value, seg.idx)
    await loadDetail(projectId.value)
    toast('分段已删除')
  } catch (e) { errToast(e) }
}

async function resynthSeg(seg) {
  if (resynthBusy.value >= 0) return
  resynthBusy.value = seg.idx
  segJob.value = { idx: seg.idx, message: '排队中…' }
  try {
    const r = await api.resynthSegment(projectId.value, seg.idx)
    pollJob(r.job_id, async (j) => {
      segJob.value = { idx: seg.idx, message: j.message || '处理中…' }
      if (j.status === 'success') {
        resynthBusy.value = -1
        segJob.value = null
        await loadDetail(projectId.value)
        toast(j.result?.note ? `第 ${seg.idx + 1} 段已重合成，但：${j.result.note}` : `第 ${seg.idx + 1} 段已重合成`)
      } else if (j.status === 'error') {
        resynthBusy.value = -1
        segJob.value = null
        errToast(`第 ${seg.idx + 1} 段重合成失败：${j.message}`)
      }
    })
  } catch (e) {
    resynthBusy.value = -1
    segJob.value = null
    errToast(e)
  }
}

async function resegment() {
  if (!confirm('按当前文稿重新切分分段？\n\n现有分段——包括你手工改过的文字和已合成的音频——会被清空重建。')) return
  splitBusy.value = true
  try {
    const r = await api.splitSegments(projectId.value)
    await loadDetail(projectId.value)
    toast(`已切为 ${r.count} 段，请核对文字后再生成声音轨`)
  } catch (e) { errToast(e) } finally { splitBusy.value = false }
}

// ---------- 分段实时试听 ----------
async function segPreview(seg) {
  if (segPreviewing.value === seg.idx) {
    segStopPreview()
    return
  }
  if (!seg.mixed_path) { toast('该段无音频', 'error'); return }
  try {
    if (!segPreviewEngine.value) segPreviewEngine.value = new PreviewEngine()
    await segPreviewEngine.value.load(api.audioUrl(seg.mixed_path) + '?t=' + Date.now())
    segPreviewEngine.value.onEnd = () => { segPreviewing.value = -1 }
    segPreviewing.value = seg.idx
    segPreviewEngine.value.play(segDraft)
  } catch (e) { errToast(`试听失败：${e.message}`) }
}

function segStopPreview() {
  if (segPreviewEngine.value) segPreviewEngine.value.stop()
  segPreviewing.value = -1
}

// 监听 segDraft 变化，实时更新正在试听的参数
watch(segDraft, () => {
  if (segPreviewing.value >= 0 && segPreviewEngine.value) {
    segPreviewEngine.value.setParams(segDraft)
  }
}, { deep: true })

// ---------- 章节 ----------
async function saveChaptersList() {
  try {
    await api.saveChapters(projectId.value, chapters.value)
    toast('章节已保存')
  } catch (e) { errToast(e) }
}

async function autoChapters() {
  await loadDetail(projectId.value)
  await saveChaptersList()
  toast('已恢复自动切分')
}

async function exportIdx(fmtName) {
  try {
    const resp = await api.exportChapters(projectId.value, fmtName, false)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${detail.value?.title || 'chapters'}.${fmtName}`
    a.click()
    URL.revokeObjectURL(url)
    toast(`${fmtName.toUpperCase()} 索引已导出`)
  } catch (e) { errToast(e) }
}

async function exportZip() {
  try {
    const r = await api.exportChapters(projectId.value, 'cue', true)
    const a = document.createElement('a')
    a.href = api.audioUrl(r.file)
    a.download = ''
    a.click()
    toast(`分章 ZIP 已生成（${r.chapters} 章），开始下载`)
  } catch (e) { errToast(e) }
}

// ---------- 对比试听 ----------
async function compareRun() {
  if (compareSel.value.length < 2) { toast('至少选择 2 个声音对比', 'error'); return }
  if (!compareText.value.trim()) { toast('请输入对比文本', 'error'); return }
  compareBusy.value = true
  compareItems.value = []
  try {
    const r = await api.compare({ voice_ids: compareSel.value, text: compareText.value, title: '对比试听' })
    pollJob(r.job_id, async (j) => {
      compareJob.value = { progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        compareBusy.value = false
        compareItems.value = j.result.items || []
        toast(`对比完成：${compareItems.value.length} 个声音`)
      } else if (j.status === 'error') {
        compareBusy.value = false
        errToast(`对比失败：${j.message}`)
      }
    })
  } catch (e) { compareBusy.value = false; errToast(e) }
}

function playCompare(startIdx) {
  const els = document.querySelectorAll('.cmp-audio')
  els.forEach((el) => { el.pause(); el.currentTime = 0 })
  const playNext = (i) => {
    if (i >= els.length) { toast('对比播放结束'); return }
    els[i].play()
    els[i].onended = () => playNext(i + 1)
  }
  playNext(startIdx)
}

onBeforeUnmount(() => {
  if (preview.value) preview.value.dispose()
  if (segPreviewEngine.value) segPreviewEngine.value.dispose()
  if (wsWave.value) wsWave.value.destroy()
  if (wsFinal.value) wsFinal.value.destroy()
})

const projectStatusText = computed(() => {
  const map = { draft: '未生成', generating: '生成中', voices_ready: '声音轨就绪', rendering: '渲染中', ready: '成品就绪', error: '失败' }
  return map[detail.value?.status] || detail.value?.status || '未选择'
})
const statusTone = computed(() =>
  ({ voices_ready: 'ok', ready: 'ok', generating: 'warn', rendering: 'warn', error: 'err', draft: '' }[detail.value?.status] || ''))

const srcName = (s) => ({ edge: 'Edge', gpt_sovits: 'GPT-SoVITS', minimax: 'MiniMax', fish: 'Fish', openvino: 'NPU' }[s] || s)
const duckingModeText = computed(() =>
  ({ off: '关闭', sidechain: '自动跟随人声', curve: '自定义曲线' }[params.ducking_mode] || params.ducking_mode))

// 四元组就绪检查：项目+文稿+数字人+参考音频全部绑定后才能生成
const canGenerate = computed(() => {
  if (!detail.value) return false
  const hasDoc = !!detail.value.document_id
  const hasVoice = !!detail.value.voice_id
  // 数字人模式需要参考音频，声音库模式不需要
  const isHuman = !!detail.value.digital_human_id
  const hasRef = !isHuman || !!detail.value.ref_audio_name
  return hasDoc && hasVoice && hasRef
})
</script>

<template>
  <div>
    <div class="page-title">🎛️ 合成工作台</div>
    <div class="page-sub">选择项目 → 绑定文稿/数字人/参考音频 → 生成声音轨 → 渲染成品（四元组缺一不可）。</div>

    <!-- Step 1: 选择/新建项目 -->
    <div class="card">
      <h3>① 选择项目</h3>
      <div class="row">
        <select v-model="projectId" style="flex:2;" @change="loadDetail(projectId)">
          <option value="">— 选择已有项目 —</option>
          <option v-for="p in store.projects" :key="p.id" :value="p.id">{{ p.title }}（{{ p.status }}）</option>
        </select>
        <input v-model="newTitle" type="text" placeholder="新项目名称" style="flex:2;" />
        <button class="btn primary" @click="quickCreate">＋ 新建</button>
        <span class="badge" :class="statusTone">{{ projectStatusText }}</span>
        <span v-if="detail?.cost > 0" class="badge accent">云费用 ¥{{ detail.cost.toFixed(4) }}</span>
      </div>
    </div>

    <!-- Step 2: 项目四元组（只读展示 + 修改入口） -->
    <template v-if="detail">
      <div class="card">
        <h3>② 项目配置</h3>
        <!-- 四元组只读展示 -->
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px;">
          <div class="info-item">
            <span class="muted" style="font-size:11px;">📄 文稿</span>
            <b>{{ detail.document_title || '未选择' }}</b>
            <span v-if="detail.document_polished" class="badge ok" style="font-size:10px;">已润色</span>
          </div>
          <div class="info-item">
            <span class="muted" style="font-size:11px;">🎭 数字人</span>
            <b>{{ detail.digital_human_name || '未关联' }}</b>
          </div>
          <div class="info-item">
            <span class="muted" style="font-size:11px;">🎤 声音模型</span>
            <b>{{ detail.voice_name || '未选择' }}</b>
            <span class="muted" style="font-size:10px;">{{ srcName(detail.voice_source) }}</span>
            <!-- checkpoint 选择 -->
            <div v-if="checkpoints.length > 1" style="margin-top:4px;">
              <select v-model="curCheckpoint" style="font-size:11px; padding:2px 6px; width:100%;" @change="onCheckpointChange">
                <option v-for="cp in checkpoints" :key="cp.name" :value="cp.name">{{ cp.name }}{{ cp.is_current ? ' ✓' : '' }}</option>
              </select>
            </div>
          </div>
          <div class="info-item">
            <span class="muted" style="font-size:11px;">🎵 推理参考音频</span>
            <b>{{ detail.ref_audio_name || '默认' }}</b>
            <span v-if="detail.ref_text_preview" class="muted" style="font-size:10px;">{{ detail.ref_text_preview }}...</span>
          </div>
        </div>
        <!-- 修改入口：折叠 -->
        <details>
          <summary class="muted" style="cursor:pointer; font-size:12px;">✏️ 修改配置</summary>
          <div style="margin-top:10px;">
            <div class="row">
              <select v-model="selDocId" style="flex:2;" @change="onDocChange">
                <option value="">— 选择文稿 —</option>
                <option v-for="d in store.documents" :key="d.id" :value="d.id">{{ d.title }}{{ d.polished ? '（已润色）' : '' }}</option>
              </select>
            </div>
            <div class="row" style="margin-top:8px;">
              <select v-model="selVoiceId" style="flex:2;" @change="onVoiceChange">
                <option value="">— 选择数字人/声音 —</option>
                <optgroup v-if="digitalHumans.length" label="🎭 数字人">
                  <option v-for="h in digitalHumans" :key="'h'+h.id" :value="'h:'+h.id">{{ h.name }}{{ h.status === 'trained' ? ' ✓' : '' }}</option>
                </optgroup>
                <optgroup label="🎤 声音库">
                  <option v-for="v in store.voices" :key="v.id" :value="v.id">{{ v.name }}（{{ v.source }}）</option>
                </optgroup>
              </select>
            </div>
            <div v-if="selVoiceId && selVoiceId.startsWith('h:')" style="margin-top:8px;">
              <div class="row">
                <select v-model="selRefAudio" @change="onRefAudioChange" style="flex:2;">
                  <option value="">自动选择（默认）</option>
                  <option v-for="(ref, i) in refAudios" :key="i" :value="i">{{ ref.label ? ref.label + ' ' : '' }}#{{ i + 1 }} {{ ref.text.slice(0, 40) }}...</option>
                </select>
              </div>
              <audio v-if="selRefAudio !== '' && refAudios[selRefAudio]" :src="refAudioUrl(selRefAudio)" controls style="width:100%; height:32px; margin-top:6px;"></audio>
              <!-- 辅助参考（多选混合音色）：勾选后与主参考融合，形成独特混合声音 -->
              <div v-if="refAudios.length" style="margin-top:8px; font-size:12px;">
                <div class="muted" style="margin-bottom:4px;">🎨 辅助参考（可多选，合成时与主参考音色融合）</div>
                <div style="max-height:110px; overflow-y:auto; border:1px solid var(--border); border-radius:6px; padding:6px 8px; background:var(--card2);">
                  <label v-for="(ref, i) in refAudios" :key="'aux' + i" style="display:flex; align-items:center; gap:6px; padding:2px 0; cursor:pointer;">
                    <input type="checkbox" :value="i" v-model="selAuxRefs" :disabled="selRefAudio === i || selRefAudio === String(i)" />
                    <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">#{{ i + 1 }} {{ ref.text.slice(0, 28) }}...</span>
                  </label>
                </div>
                <div class="muted" style="margin-top:4px; font-size:11px;">提示：主参考决定基础音色，勾选的辅助参考按比例融入——想混合「自己 + Edge 磁性」，主参考选一个、辅助参考勾另一个即可</div>
              </div>
            </div>
          </div>
        </details>
      </div>

      <!-- Step 3: 生成声音轨 -->
      <div class="card">
        <h3>③ 生成声音轨</h3>
        <div class="row">
          <button class="btn success" :disabled="genBusy || !canGenerate" @click="generate(false)">🔊 生成声音轨（{{ detail.segments?.length || 0 }} 段）</button>
          <button v-if="segReady && segTodo" class="btn" :disabled="genBusy || !canGenerate"
                  title="跳过已完成的段，只补做剩下的（重做全部会换随机种子，已听过的版本会变）"
                  @click="generate(true)">▶ 继续未完成的 {{ segTodo }} 段</button>
          <label v-if="verifyMode" class="muted" style="font-size:12px;">合成后文字校验
            <select v-model="verifyMode" @change="saveVerifyMode" style="font-size:12px;">
              <option value="full">全部句子（最准最慢）</option>
              <option value="smart">仅短句与对话（推荐）</option>
              <option value="off">关闭（只看时长）</option>
            </select>
          </label>
          <span v-if="!canGenerate" class="muted" style="font-size:12px; color:#e74c3c;">请先完成：{{ !detail.document_id ? '选择文稿' : '' }}{{ !detail.voice_id ? ' 选择数字人' : '' }}{{ detail.digital_human_id && !detail.ref_audio_name ? ' 选择参考音频' : '' }}</span>
          <label class="btn" style="cursor:pointer;">
            🎵 上传背景音乐
            <input type="file" accept="audio/*" style="display:none" @change="onBgm" />
          </label>
          <span v-if="detail.bgm_path" class="badge ok">背景乐已上传</span>
        </div>
        <template v-if="genJob">
          <div class="progress"><div :style="{ width: (genJob.progress || 0) + '%' }"></div></div>
          <div class="job-line">⚙️ {{ genJob.message }}</div>
        </template>
        <div v-if="detail.voice_track_path" id="wave-vt" class="wave-wrap" style="margin-top:8px;"></div>
        <div v-if="detail.voice_track_path" style="margin-top:8px;">
          <audio :src="api.audioUrl(detail.voice_track_path) + '?t=' + detail._ts" controls style="width:100%; height:36px;"></audio>
          <a class="btn small" style="margin-left:8px;" :href="api.audioUrl(detail.voice_track_path)" download>⬇ 下载声音轨</a>
        </div>

        <!-- 分段：合成前可逐段改文字 / 删除 -->
        <template v-if="detail.segments?.length">
          <div class="row" style="justify-content:space-between; margin:14px 0 8px;">
            <b style="font-size:13px;">📑 文稿分片（{{ detail.segments.length }} 段，每段各合成一次）</b>
            <div class="row" style="gap:8px;">
              <span class="muted" style="font-size:11px;">逐段改文字 / 删掉不要的段</span>
              <button class="btn small" :disabled="splitBusy" @click="resegment">
                {{ splitBusy ? '切分中…' : '↻ 按最新文稿重新切分' }}
              </button>
            </div>
          </div>
          <div v-if="detail.split_stale" class="job-line" style="color:#d97706; margin-bottom:8px;">
            ⚠️ 文稿内容与现有分段不一致：合成将使用下面这些分段的文字。要按最新文稿重切请点「重新切分」（会清空手工编辑）。
          </div>
          <div v-for="seg in detail.segments" :key="seg.idx" class="list-item" style="padding:8px 12px; align-items:flex-start;">
            <div style="flex:1; min-width:0;">
              <div class="row" style="gap:8px;">
                <span class="badge accent">#{{ seg.idx + 1 }}</span>
                <span v-if="editingSeg !== seg.idx" class="sub" style="flex:1;">{{ seg.text.slice(0, 60) }}{{ seg.text.length > 60 ? '…' : '' }}</span>
                <span v-else class="sub" style="flex:1;">编辑第 {{ seg.idx + 1 }} 段（{{ seg.text.length }} 字）</span>
                <span v-if="seg.status === 'ready' && !seg.error" class="muted" style="font-size:11px;">{{ seg.text.length }} 字</span>
                <span class="badge" :class="seg.status === 'ready' ? 'ok' : seg.status === 'error' ? 'err' : ''">{{ seg.status }}</span>
                <span v-if="seg.status === 'ready' && seg.error" class="badge warn" :title="seg.error">⚠ 已降级合成</span>
                <span v-if="hasSegParams(seg)" class="badge warn">自定义</span>
                <span v-if="seg.offset" class="muted mono">{{ fmt(seg.offset) }}</span>
              </div>
              <div v-if="editingSeg === seg.idx" style="margin-top:8px;">
                <textarea v-model="editDraft" rows="3" style="width:100%; box-sizing:border-box; font-size:13px;"></textarea>
                <div class="row" style="gap:6px; margin-top:6px;">
                  <button class="btn small primary" @click="saveSegText(seg)">💾 保存这段文字</button>
                  <button class="btn small" @click="editingSeg = -1">取消</button>
                  <span class="muted" style="font-size:11px;">保存后本段旧音频作废，点「重合成这段」即可只重做这一段</span>
                </div>
              </div>
              <div v-if="segJob && segJob.idx === seg.idx" class="job-line" style="margin-top:6px;">⚙️ {{ segJob.message }}</div>
              <div v-if="expandedSeg === seg.idx" class="slider-grid" style="margin-top:10px;">
                <div class="slider-item">
                  <label>语速 <b>{{ segDraft.speed.toFixed(2) }}×</b></label>
                  <input type="range" min="0.7" max="1.6" step="0.05" v-model.number="segDraft.speed" />
                </div>
                <div class="slider-item">
                  <label>音调 <b>{{ segDraft.pitch > 0 ? '+' : '' }}{{ segDraft.pitch }} 半音</b></label>
                  <input type="range" min="-6" max="6" step="0.5" v-model.number="segDraft.pitch" />
                </div>
                <div class="slider-item">
                  <label>音量 <b>{{ segDraft.volume.toFixed(2) }}×</b></label>
                  <input type="range" min="0" max="2" step="0.05" v-model.number="segDraft.volume" />
                </div>
                <div class="slider-item">
                  <label>低频 EQ <b>{{ segDraft.eq.low > 0 ? '+' : '' }}{{ segDraft.eq.low }} dB</b></label>
                  <input type="range" min="-12" max="12" step="1" v-model.number="segDraft.eq.low" />
                </div>
                <div class="slider-item">
                  <label>中频 EQ <b>{{ segDraft.eq.mid > 0 ? '+' : '' }}{{ segDraft.eq.mid }} dB</b></label>
                  <input type="range" min="-12" max="12" step="1" v-model.number="segDraft.eq.mid" />
                </div>
                <div class="slider-item">
                  <label>高频 EQ <b>{{ segDraft.eq.high > 0 ? '+' : '' }}{{ segDraft.eq.high }} dB</b></label>
                  <input type="range" min="-12" max="12" step="1" v-model.number="segDraft.eq.high" />
                </div>
              </div>
            </div>
            <div class="actions" style="flex-direction:column; align-items:flex-end; gap:6px;">
              <div class="row" style="gap:6px;">
                <button class="btn small" @click="openSegParams(seg)">{{ expandedSeg === seg.idx ? '收起' : '参数' }}</button>
                <button class="btn small" @click="editSegText(seg)">{{ editingSeg === seg.idx ? '收起编辑' : '改文字' }}</button>
                <button class="btn small primary" :disabled="resynthBusy >= 0 || genBusy" :title="resynthBusy === seg.idx ? '' : '只重做这一段，其余段音频不动，随后自动重拼声音轨'" @click="resynthSeg(seg)">{{ resynthBusy === seg.idx ? '合成中…' : '重合成这段' }}</button>
                <button class="btn small danger" title="删除这一整段（不再合成）" @click="delSeg(seg)">删除</button>
                <button v-if="expandedSeg === seg.idx" class="btn small primary" @click="segPreview(seg)">{{ segPreviewing === seg.idx ? '⏹ 停止' : '▶ 试听' }}</button>
                <button v-if="expandedSeg === seg.idx" class="btn small primary" @click="applySegParams(seg)">应用</button>
                <button v-if="hasSegParams(seg)" class="btn small" @click="clearSegParams(seg)">清除</button>
              </div>
              <audio v-if="seg.mixed_path" :src="api.audioUrl(seg.mixed_path) + '?t=' + detail._ts" controls style="width:210px; height:30px;"></audio>
            </div>
          </div>
        </template>
      </div>

      <div class="card">
        <h3>④ 混音参数微调 <span class="muted" style="font-size:12px; font-weight:400;">（滑块实时生效，浏览器本地混音试听，零等待）</span></h3>
        <!-- 预设按钮 -->
        <div class="row" style="gap:8px; margin-bottom:14px;">
          <span class="muted" style="font-size:12px; line-height:32px;">一键预设：</span>
          <button class="btn small" @click="applyPreset('rich')">🔊 饱满磁性</button>
          <button class="btn small" @click="applyPreset('warm')">🌿 温暖柔和</button>
          <button class="btn small" @click="applyPreset('clear')">✨ 清晰明亮</button>
          <button class="btn small" @click="applyPreset('neutral')">↺ 默认</button>
        </div>
        <div class="slider-grid">
          <div class="slider-item">
            <label>语速 <b>{{ params.speed.toFixed(2) }}×</b></label>
            <input type="range" min="0.7" max="1.6" step="0.05" v-model.number="params.speed" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>音调 <b>{{ params.pitch > 0 ? '+' : '' }}{{ params.pitch }} 半音</b></label>
            <input type="range" min="-6" max="6" step="0.5" v-model.number="params.pitch" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>人声音量 <b>{{ params.volume.toFixed(2) }}×</b></label>
            <input type="range" min="0" max="2" step="0.05" v-model.number="params.volume" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>低频 EQ <b>{{ params.eq.low > 0 ? '+' : '' }}{{ params.eq.low }} dB</b></label>
            <input type="range" min="-12" max="12" step="1" v-model.number="params.eq.low" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>中频 EQ <b>{{ params.eq.mid > 0 ? '+' : '' }}{{ params.eq.mid }} dB</b></label>
            <input type="range" min="-12" max="12" step="1" v-model.number="params.eq.mid" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>高频 EQ <b>{{ params.eq.high > 0 ? '+' : '' }}{{ params.eq.high }} dB</b></label>
            <input type="range" min="-12" max="12" step="1" v-model.number="params.eq.high" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>背景乐音量 <b>{{ (params.bgm_volume * 100).toFixed(0) }}%</b></label>
            <input type="range" min="0" max="1" step="0.05" v-model.number="params.bgm_volume" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>背景乐闪避 <b>{{ duckingModeText }}</b> <span class="muted" style="font-weight:400;">（仅渲染生效）</span></label>
            <select v-model="params.ducking_mode" @change="saveParams" style="padding:6px 8px;">
              <option value="off">关闭</option>
              <option value="sidechain">自动跟随人声（侧链压缩）</option>
              <option value="curve">自定义闪避曲线</option>
            </select>
          </div>
          <div v-if="params.ducking_mode === 'sidechain'" class="slider-item">
            <label>闪避强度 <b>{{ (params.ducking * 100).toFixed(0) }}%</b></label>
            <input type="range" min="0" max="1" step="0.05" v-model.number="params.ducking" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>段落间隔 <b>{{ params.gap.toFixed(1) }}s</b></label>
            <input type="range" min="0" max="3" step="0.1" v-model.number="params.gap" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>开头淡入 <b>{{ params.fade_in.toFixed(1) }}s</b> <span class="muted" style="font-weight:400;">（仅渲染生效）</span></label>
            <input type="range" min="0" max="10" step="0.5" v-model.number="params.fade_in" @change="saveParams" />
          </div>
          <div class="slider-item">
            <label>结尾淡出 <b>{{ params.fade_out.toFixed(1) }}s</b> <span class="muted" style="font-weight:400;">（仅渲染生效）</span></label>
            <input type="range" min="0" max="10" step="0.5" v-model.number="params.fade_out" @change="saveParams" />
          </div>
        </div>

        <!-- 自定义闪避曲线 -->
        <div v-if="params.ducking_mode === 'curve'" style="margin-top:12px; border:1px dashed var(--border); border-radius:10px; padding:12px;">
          <div class="muted" style="font-size:12px; margin-bottom:8px;">
            📉 闪避深度曲线（横轴为音频位置 0/25/50/75/100%，纵轴深度越高背景乐压得越弱；渲染时按此曲线逐帧调制）
          </div>
          <div class="row">
            <div v-for="(kf, i) in params.ducking_curve" :key="i" class="slider-item" style="flex:1; min-width:120px;">
              <label>{{ kf.pct }}% 位置 <b>{{ (kf.level * 100).toFixed(0) }}%</b></label>
              <input type="range" min="0" max="1" step="0.05" v-model.number="kf.level" @change="saveParams" />
            </div>
          </div>
          <div class="row" style="margin-top:6px;">
            <button class="btn small" @click="params.ducking_curve.forEach(k => (k.level = 0)); saveParams()">清零</button>
            <button class="btn small" @click="params.ducking_curve.forEach(k => (k.level = 0.5)); saveParams()">均匀 50%</button>
            <span class="muted" style="font-size:11px;">💡 常见用法：开头结尾浅、中间深（随人声朗读起伏）</span>
          </div>
        </div>

        <div class="row" style="margin-top:14px;">
          <button class="btn primary" :disabled="previewing || !detail.voice_track_path" @click="startPreview">
            {{ previewing ? '⏹ 停止试听' : '▶ 实时试听（带效果）' }}
          </button>
          <button v-if="previewing" class="btn" @click="stopPreview">停止</button>
          <span v-if="previewing" class="muted">已播放 {{ previewTime }}</span>
          <span class="muted" style="margin-left:auto;">💡 试听在浏览器内实时混音；分段参数/闪避曲线/淡入淡出仅在渲染时生效</span>
        </div>
      </div>

      <div class="card">
        <h3>⑤ 渲染成品</h3>
        <div class="row">
          <button class="btn success" :disabled="renderBusy || !detail.voice_track_path || !canGenerate" @click="render">🎬 渲染最终作品</button>
          <span v-if="!canGenerate" class="muted" style="font-size:12px; color:#e74c3c;">四元组未就绪</span>
          <a v-if="detail.final_path" class="btn" :href="api.audioUrl(detail.final_path)" download>⬇ 下载成品</a>
          <span v-if="detail.duration" class="muted">时长 {{ Math.floor(detail.duration / 60) }}分{{ Math.round(detail.duration % 60) }}秒</span>
        </div>
        <template v-if="renderJob">
          <div class="progress"><div :style="{ width: (renderJob.progress || 0) + '%' }"></div></div>
          <div class="job-line">⚙️ {{ renderJob.message }}</div>
        </template>
        <div v-if="detail.final_path" class="hint" style="margin-top:8px;">成品（在线播放，可拖动进度条）：</div>
        <div v-if="detail.final_path" id="wave-final" class="wave-wrap" style="margin-top:8px;"></div>
        <div v-if="detail.final_path" style="margin-top:8px;">
          <audio :src="api.audioUrl(detail.final_path) + '?t=' + detail._ts" controls style="width:100%; height:36px;"></audio>
          <a class="btn small" style="margin-left:8px;" :href="api.audioUrl(detail.final_path)" download>⬇ 下载成品</a>
        </div>
      </div>

      <div class="card">
        <h3>⑥ 章节书签导出</h3>
        <div class="hint">
          生成声音轨时自动切分章节（识别「第X章/回」标题；无标题则每 10 段一组）。可改名、调整范围后导出
          CUE（CD/播放器索引）/ LRC（歌词时间轴）/ JSON（结构化），或打包分章 mp3。
        </div>
        <div v-if="!chapters.length" class="empty">暂无章节，请先生成声音轨。</div>
        <template v-else>
          <div v-for="(ch, i) in chapters" :key="i" class="row" style="margin-bottom:6px;">
            <span class="mono muted" style="width:110px;">{{ fmt(ch.start) }} → {{ fmt(ch.end) }}</span>
            <input v-model="ch.title" type="text" style="flex:1;" />
            <span class="muted" style="font-size:11px; white-space:nowrap;">段 {{ ch.start_seg + 1 }}–{{ ch.end_seg + 1 }}</span>
          </div>
          <div class="row" style="margin-top:10px;">
            <button class="btn small" @click="saveChaptersList">💾 保存章节</button>
            <button class="btn small" @click="autoChapters">↺ 自动重切</button>
            <span style="flex:1;"></span>
            <button class="btn small" @click="exportIdx('cue')">CUE 索引</button>
            <button class="btn small" @click="exportIdx('lrc')">LRC 歌词</button>
            <button class="btn small" @click="exportIdx('json')">JSON</button>
            <button class="btn small success" :disabled="!detail.final_path" @click="exportZip">📦 分章 ZIP（mp3）</button>
          </div>
          <div class="hint" style="margin-top:8px;">分章 ZIP 需要先渲染成品；未渲染时会回退使用声音轨切分。</div>
        </template>
      </div>
    </template>

    <div class="card">
      <h3>⚡ 多人音色对比试听</h3>
      <div class="hint">同一段文本用多个声音分别合成，A/B 连续播放对比，选出最合适的音色。云端引擎会按字符计费（单价见设置）。</div>
      <div class="row" style="gap:8px;">
        <label v-for="v in store.voices" :key="v.id" class="row" style="gap:6px; cursor:pointer; padding:5px 12px; border:1px solid var(--border); border-radius:20px;"
          :style="compareSel.includes(v.id) ? { borderColor: 'var(--accent)', background: 'rgba(108,140,255,.12)' } : {}">
          <input type="checkbox" :value="v.id" v-model="compareSel" style="accent-color:var(--accent);" />
          {{ v.name }} <span class="muted" style="font-size:11px;">{{ srcName(v.source) }}</span>
        </label>
      </div>
      <div class="row" style="margin-top:10px;">
        <input v-model="compareText" type="text" placeholder="对比文本（例：大家好，欢迎收听本期节目，我们马上开始。）" style="flex:1;" @keyup.enter="compareRun" />
        <button class="btn primary" :disabled="compareBusy || compareSel.length < 2" @click="compareRun">生成对比</button>
      </div>
      <template v-if="compareJob">
        <div class="progress"><div :style="{ width: (compareJob.progress || 0) + '%' }"></div></div>
        <div class="job-line">⚙️ {{ compareJob.message }}</div>
      </template>
      <div v-if="compareItems.length" class="slider-grid" style="margin-top:12px;">
        <div v-for="(it, i) in compareItems" :key="i" class="card" style="margin:0;">
          <div class="row" style="justify-content:space-between;">
            <b>{{ it.voice_name }}</b>
            <span class="badge accent">{{ srcName(it.voice_source) }}</span>
          </div>
          <div v-if="it.error" class="err-text" style="margin-top:6px;">{{ it.error }}</div>
          <audio v-else :src="api.audioUrl(it.path)" controls class="cmp-audio" style="margin-top:6px;"></audio>
          <div class="row" style="margin-top:6px; justify-content:space-between;">
            <button class="btn small" @click="playCompare(i)">▶ A/B 连播</button>
            <span class="muted" style="font-size:11px;">{{ it.cost > 0 ? '¥' + it.cost.toFixed(4) : '免费' }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
