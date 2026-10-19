<script setup>
import { ref, reactive, onMounted, watch } from 'vue'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { refreshAll, pollJob } from '../store.js'

const humans = ref([])
const showCreate = ref(false)
const editingId = ref(null)
const form = reactive({ name: '', voice_id: '', desc: '' })
const activeTab = ref('overview')
const detail = ref(null)
const trainBusy = ref('')
const trainJob = ref(null)
const trainHistory = ref([])
const duplicates = ref(null)
const baseModel = ref('')
const trainingData = ref([])
const selectedPreset = ref('')
const baseModels = ref([])
const selectedBaseModel = ref('')
const trainLogs = ref([]) // 训练日志缓冲（最近50条）
const refAudios = ref([]) // 可用参考音频列表
const selectedRefAudio = ref('') // 当前选中的参考音频索引
const dataPage = ref(1)
const dataSize = ref(20)
const dataPages = ref(1)
const dataTotal = ref(0)
const dataSource = ref('main') // main=主数据 / s1=语感数据 / s2=音色数据
// 切分方式：silence=按静音（快）；align=先整条识别拿标点时间戳，只在句子边界下刀且自带标注
const sliceOpts = reactive({ mode: 'align', granularity: 'para', hotwords: '' })
const dataSourceLabel = { main: '主数据', s1: '🗣 s1 语感数据', s2: '🎤 s2 音色数据' }

onMounted(async () => {
  humans.value = await api.humans()
  if (store.voices.length) await refreshAll()
  if (humans.value.length) {
    const lastId = localStorage.getItem('ac_human')
    const target = humans.value.find((h) => h.id === lastId) || humans.value[0]
    await loadHuman(target.id)
    // 检查是否有正在进行的训练 job
    await checkRunningJob(target.id)
  }
})

async function loadRefAudios() {
  try {
    const data = await api.humanDataList(detail.value.id, 1, 500)
    refAudios.value = (data.items || []).filter((item) => item.text && item.text.trim())
  } catch (e) { refAudios.value = [] }
}

async function onRefAudioChange() {
  if (selectedRefAudio.value === '' || !refAudios.value[selectedRefAudio.value]) return
  const ref = refAudios.value[selectedRefAudio.value]
  try {
    await api.updateHuman(detail.value.id, {
      voice_config: {
        ref_path: detail.value.data_dir + '/' + ref.audio,
        ref_text: ref.text,
        ref_lang: 'zh',
      }
    })
    toast('参考音频已设置')
  } catch (e) { errToast(e) }
}

async function checkRunningJob(humanId) {
  try {
    const jobs = await api.jobs()
    const humanName = detail.value?.name || ''
    const running = jobs.find((j) => j.status === 'running' && j.kind === 'human_train' && j.name?.includes(humanName))
    if (running) {
      trainBusy.value = true
      toast('⏳ 检测到正在进行的训练任务，等待完成…')
      trainJob.value = { progress: running.progress || 0, message: running.message || '训练中…', status: 'running' }
      pollJob(running.id, async (j) => {
        trainJob.value = { progress: j.progress, message: j.message, status: j.status }
        if (j.message && (trainLogs.value.length === 0 || trainLogs.value[trainLogs.value.length - 1] !== j.message)) {
          trainLogs.value.push(j.message)
          if (trainLogs.value.length > 80) trainLogs.value.splice(0, trainLogs.value.length - 50)
        }
        if (j.status === 'success') {
          trainBusy.value = false
          await api.updateHuman(humanId, { model_s2: j.result?.sovits_model || '', model_gpt: j.result?.gpt_model || '', status: 'trained' })
          await loadHuman(humanId)
          toast('数字人训练完成！🎭')
        } else if (j.status === 'error') {
          trainBusy.value = false
          await api.updateHuman(humanId, { status: 'untrained' })
          errToast(`训练失败：${j.message}`)
          await loadHuman(humanId)
        }
      })
    }
  } catch (e) { /* 忽略 */ }
}
import { store } from '../store.js'

async function loadHuman(id) {
  try {
    detail.value = await api.human(id)
    // 规范化训练配置：保证 sovits/gpt 结构存在（旧数字人或空配置为 {} 时，
    // 训练配置页 v-model 会因 sovits 为 undefined 报错黑屏）
    const tc = detail.value.training_config || {}
    detail.value.training_config = {
      sovits: { batch_size: 4, epochs: 15, lr: 0.0001, fp16: true, ...(tc.sovits || {}) },
      gpt: { batch_size: 4, epochs: 15, lr: 0.0001, dpo: false, ...(tc.gpt || {}) },
    }
    // 规范化推理参数（空对象时推理参数页同样会黑屏）
    const ip = detail.value.inference_params || {}
    detail.value.inference_params = {
      temperature: 0.72, top_p: 0.85, top_k: 15, repetition_penalty: 1.05, ...ip,
    }
    editingId.value = id
    activeTab.value = 'overview'
    trainHistory.value = await api.humanHistory(id).catch(() => [])
    duplicates.value = await api.humanCheckDuplicates(id).catch(() => null)
    baseModels.value = await api.humanBaseModels(id).catch(() => [])
    selectedBaseModel.value = ''
    localStorage.setItem('ac_human', id)
    selectedPreset.value = detectPreset(detail.value.polish_params)
    await loadRefAudios()
  } catch (e) { errToast(e) }
}

async function createHuman() {
  try {
    const h = await api.createHuman(form)
    humans.value = await api.humans()
    showCreate.value = false
    toast(`数字人「${h.name}」已创建`)
    await loadHuman(h.id)
  } catch (e) { errToast(e) }
}

async function updateHuman() {
  try {
    await api.updateHuman(editingId.value, {
      name: detail.value.name,
      desc: detail.value.desc,
      voice_id: detail.value.voice_id,
      data_dir: detail.value.data_dir,
      s1_data_dir: detail.value.s1_data_dir || '',
      s2_data_dir: detail.value.s2_data_dir || '',
      model_s2: detail.value.model_s2,
      model_gpt: detail.value.model_gpt,
      status: detail.value.status,
      polish_params: detail.value.polish_params,
      training_config: detail.value.training_config,
      inference_params: detail.value.inference_params,
    })
    humans.value = await api.humans()
    // 重新加载详情确保数据同步
    detail.value = await api.human(editingId.value)
    trainHistory.value = await api.humanHistory(editingId.value).catch(() => [])
    toast('保存成功 ✓')
  } catch (e) { errToast(e) }
}

async function deleteHuman(h) {
  if (!confirm(`确定删除数字人「${h.name}」？相关配置将被移除。`)) return
  try {
    await api.deleteHuman(h.id)
    humans.value = await api.humans()
    if (detail.value?.id === h.id) detail.value = null
    toast('数字人已删除')
  } catch (e) { errToast(e) }
}

function qualityColor(score) {
  if (score >= 70) return 'ok'
  if (score >= 40) return 'warn'
  return 'err'
}

function qualityLabel(score) {
  if (score >= 70) return '数据质量优秀'
  if (score >= 40) return '数据质量一般'
  if (score > 0) return '数据质量不足'
  return '未训练'
}

const presets = {
  '文学朗读': { temperature: 0.72, top_p: 0.85, top_k: 15, repetition_penalty: 1.05 },
  '稳定朗读': { temperature: 0.6, top_p: 0.8, top_k: 10, repetition_penalty: 1.05 },
  '生动演绎': { temperature: 0.85, top_p: 0.9, top_k: 20, repetition_penalty: 1.0 },
}

const polishPresets = {
  '饱满磁性': { eq: { low: 4, mid_low: 2, mid: 0, mid_high: 1, high: 2 }, compressor: { enabled: true, threshold: -22, ratio: 3.5, attack: 10, release: 200, makeup: 4 }, reverb_amount: 25, reverb_decay: 1.5, highpass: 80, lowpass: 15000 },
  '清晰明亮': { eq: { low: 0, mid_low: 1, mid: 2, mid_high: 3, high: 3 }, compressor: { enabled: true, threshold: -25, ratio: 3, attack: 10, release: 200, makeup: 3 }, reverb_amount: 15, reverb_decay: 1.2, highpass: 100, lowpass: 16000 },
  '柔和温暖': { eq: { low: 3, mid_low: 1, mid: 0, mid_high: -1, high: 1 }, compressor: { enabled: true, threshold: -28, ratio: 2.5, attack: 15, release: 250, makeup: 2 }, reverb_amount: 35, reverb_decay: 2.0, highpass: 100, lowpass: 12000 },
}

function applyPolishPreset(name) {
  if (name && polishPresets[name]) {
    detail.value.polish_params = { ...polishPresets[name] }
    selectedPreset.value = name
  } else {
    selectedPreset.value = ''
  }
}

// 根据当前 polish_params 识别预设
function detectPreset(params) {
  if (!params || Object.keys(params).length === 0) return ''
  for (const [name, preset] of Object.entries(polishPresets)) {
    const match = JSON.stringify(preset) === JSON.stringify(params)
    if (match) return name
  }
  return ''
}

async function loadTrainingData(p) {
  if (!detail.value?.id) return
  dataPage.value = p || dataPage.value
  const r = await api.humanDataList(detail.value.id, dataPage.value, dataSize.value, dataSource.value).catch(() => ({ items: [], count: 0, pages: 1 }))
  trainingData.value = (r.items || []).map((item) => ({ ...item, saved: !!item.text }))
  dataTotal.value = r.count || 0
  dataPages.value = r.pages || 1
  dataPage.value = r.page || 1
}

function switchDataSource(src) {
  if (dataSource.value === src) return
  dataSource.value = src
  loadTrainingData(1)
}

async function saveText(item) {
  try {
    await api.humanSaveText(detail.value.id, item.idx, item.text, item.audio, dataSource.value)
    item.saved = true
    toast(`第 ${item.idx + 1} 条已保存`)
  } catch (e) { errToast(e) }
}

// 输入防抖自动保存：停止输入 600ms 后自动保存
const saveTimers = {}
function onTextInput(item) {
  item.saved = false
  if (saveTimers[item.idx]) clearTimeout(saveTimers[item.idx])
  saveTimers[item.idx] = setTimeout(() => {
    if (item.text && item.text.trim()) {
      saveText(item)
    }
  }, 600)
}

async function deleteSlice(item) {
  if (!confirm(`删除切片 #${item.idx + 1}（${item.audio}）？\n杂音/无效片段将从${dataSourceLabel[dataSource.value]}中移除。`)) return
  try {
    await api.humanSliceDelete(detail.value.id, item.audio, dataSource.value)
    // 重新加载列表，确保 idx 与 train.list 行号重新对齐
    await loadTrainingData()
    toast('切片已删除')
  } catch (e) { errToast(e) }
}

const uploadBusy = ref(false)
async function uploadAudio(e) {
  const f = e.target.files?.[0]
  if (!f || !detail.value) return
  const src = dataSource.value
  // 主数据需要已设置目录；s1/s2 由后端自动创建独立目录
  if (src === 'main' && !detail.value.data_dir) {
    toast('❌ 请先填写训练数据目录路径并保存', 'error')
    return
  }
  // 已有旧数据时询问用户：替换（清空重来）还是追加（保留旧数据）
  let mode = 'replace'
  try {
    const dl = await api.humanDataList(detail.value.id, 1, 1, src)
    if (dl.count > 0) {
      mode = confirm(`⚠️ ${dataSourceLabel[src]}已有 ${dl.count} 条旧数据\n\n点「确定」→ 清空旧数据，重新上传（替换）\n点「取消」→ 保留旧数据，追加上传`) ? 'replace' : 'append'
    }
  } catch (err) { /* 检测失败则默认替换模式 */ }
  uploadBusy.value = true
  try {
    // 先保存目录设置（仅主数据需要）
    if (src === 'main') await api.updateHuman(detail.value.id, { data_dir: detail.value.data_dir })
    const r = await api.humanDataUpload(detail.value.id, f, mode, src)
    if (r.archive) {
      toast(`📦 压缩包解压完成，找到 ${r.extracted} 个音频，正在逐个切片…`)
    } else {
      toast(`音频已上传（${r.duration}s）${mode === 'append' ? '，追加模式保留旧数据' : '，替换模式已清空旧数据'}，正在切片…`)
    }
    const p = await api.humanDataProcess(detail.value.id, mode, src, sliceOpts)
    toast(`${p.message}`)
    await loadTrainingData()
  } catch (e) { errToast(e) }
  finally { uploadBusy.value = false; e.target.value = '' }
}

// 处理目录内已有音频（手动放文件到 s1/s2 目录后调用：切片+ASR）
async function processDir() {
  if (!detail.value?.id) return
  uploadBusy.value = true
  try {
    await updateHuman() // 先保存目录设置
    const p = await api.humanDataProcess(detail.value.id, 'replace', dataSource.value, sliceOpts)
    toast(`${p.message}`)
    await loadTrainingData()
  } catch (e) { errToast(e) }
  finally { uploadBusy.value = false }
}

// 统计各数据源切片数量（用于训练配置页展示）
async function sourceCounts() {
  const out = { main: 0, s1: 0, s2: 0 }
  for (const s of ['main', 's1', 's2']) {
    try {
      const r = await api.humanDataList(detail.value.id, 1, 1, s)
      out[s] = r.count || 0
    } catch (e) { /* ignore */ }
  }
  return out
}

watch(activeTab, (tab) => {
  if (tab === 'data' && detail.value?.id) {
    loadTrainingData()
  }
})

async function startTraining() {
  // 检查训练状态
  if (detail.value.status === 'training') {
    toast('⏳ 训练正在进行中，请等待完成后再发起新一轮训练', 'error')
    return
  }
  if (trainBusy.value) {
    toast('⏳ 前一个训练任务尚未结束', 'error')
    return
  }
  if (!detail.value?.voice_id) { toast('❌ 请先绑定声音模型（在「概况」页设置）', 'error'); return }

  // 检查训练数据：主/s1/s2 任一有数据即可（s1/s2 独立数据不要求主目录）
  if (!detail.value.data_dir && !detail.value.s1_data_dir && !detail.value.s2_data_dir) {
    toast('❌ 未设置训练数据', 'error', 6000)
    activeTab.value = 'data'
    toast('请先在「训练数据」标签页上传音频（主/s1/s2 任一）', 'info', 8000)
    return
  }

  const q = await api.humanDataQuality(detail.value.id).catch(() => null)
  if (q?.error) {
    toast(`❌ ${q.error}`, 'error', 6000)
    activeTab.value = 'data'
    return
  }
  if (q && q.total_minutes > 0 && q.total_minutes < 10) {
    if (!confirm(`训练数据仅 ${q.total_minutes} 分钟，效果可能不理想。\n\n建议至少 1 小时录音（散文+诗歌+小说+古文混合），\n最低可用 10 分钟。\n\n确定继续吗？`)) return
  }
  // 三源全空才拦截（q 只统计主数据，s1/s2 独立数据需另行检查）
  const counts = await sourceCounts()
  if (q && q.total_minutes === 0 && q.total_files === 0 && counts.s1 === 0 && counts.s2 === 0) {
    toast('❌ 训练数据为空，请先上传朗读录音', 'error', 6000)
    activeTab.value = 'data'
    return
  }

  // 检查数据重复
  const dups = await api.humanCheckDuplicates(detail.value.id).catch(() => null)
  if (dups && dups.overlapping > 0) {
    const pct = (dups.ratio * 100).toFixed(1)
    if (dups.ratio > 0.5) {
      toast(`❌ 重复比例过高（${pct}%，${dups.overlapping}/${dups.total} 文件与历史训练重叠）。请清理或更换数据。`, 'error', 10000)
      return
    }
    if (!confirm(`⚠️ 检测到 ${pct}% 重复数据（${dups.overlapping}/${dups.total} 文件与历史训练重叠）\n\n重复数据会降低模型泛化能力。\n\n确定继续吗？`)) return
  }

  trainBusy.value = true
  try {
    // 先保存当前配置
    await updateHuman()
    // 启动数字人独立训练（从 data_dir 读数据，不依赖声音库）
    const r = await api.trainHuman(detail.value.id, {
      s2_epochs: detail.value.training_config?.sovits?.epochs || 10,
      s1_epochs: detail.value.training_config?.gpt?.epochs || 10,
      base_model: selectedBaseModel.value,
    })
    trainLogs.value = []
    trainJob.value = { progress: 0, message: '排队中…' }
    pollJob(r.job_id, async (j) => {
      trainJob.value = { progress: j.progress, message: j.message, status: j.status }
      // 收集训练日志（去重：同一条消息不重复添加）
      if (j.message && (trainLogs.value.length === 0 || trainLogs.value[trainLogs.value.length - 1] !== j.message)) {
        trainLogs.value.push(j.message)
        if (trainLogs.value.length > 80) trainLogs.value.splice(0, trainLogs.value.length - 50)
      }
      if (j.status === 'success') {
        trainBusy.value = false
        // 更新数字人状态为已训练
        await api.updateHuman(detail.value.id, {
          model_s2: j.result?.sovits_model || '',
          model_gpt: j.result?.gpt_model || '',
          status: 'trained',
        })
        detail.value.status = 'trained'
        detail.value.model_s2 = j.result?.sovits_model || ''
        detail.value.model_gpt = j.result?.gpt_model || ''
        toast('数字人训练完成！现在可以用它朗读任意文学作品了 🎭')
      } else if (j.status === 'error') {
        trainBusy.value = false
        detail.value.status = 'untrained'
        errToast(`训练失败：${j.message}`)
      }
    })
  } catch (e) { trainBusy.value = false; errToast(e) }
}
</script>

<template>
  <div>
    <div class="page-title">🎭 数字人</div>
    <div class="page-sub">
      打造专属数字人：以自己声音为基准，经过人声分离（提取参考音频）→ 音色润色（更饱满磁性）→
      GPT-SoVITS 微调训练，最终输入任意文学作品，都像自己朗读。
    </div>

    <!-- 创建数字人 -->
    <div class="card">
      <div class="row" style="justify-content:space-between;">
        <h3 style="margin:0;">⚡ 创建数字人</h3>
        <button class="btn primary" @click="showCreate = true">＋ 新建数字人</button>
      </div>
      <div class="hint">创建数字人后：绑定声音模型（声音库克隆的音色）→ 设置润色参数 → 绑定训练数据 → 训练专属模型 → 用它朗读任意文学作品。</div>
    </div>

    <!-- 数字人列表 -->
    <div v-if="!humans.length" class="empty">还没有数字人，点击上方创建。</div>
    <div v-for="h in humans" :key="h.id" class="list-item" style="padding:12px 16px; cursor:pointer;" @click="loadHuman(h.id)" :style="detail?.id === h.id ? 'border-color:var(--accent)' : ''">
      <div style="flex:1; min-width:0;">
        <div class="row" style="gap:8px;">
          <span class="name" style="font-size:16px;">🎭 {{ h.name }}</span>
          <span class="badge" :class="h.status === 'trained' ? 'ok' : h.status === 'training' ? 'warn' : ''">{{ { untrained: '待训练', training: '训练中', trained: '已训练', active: '活跃中' }[h.status] || h.status }}</span>
          <span class="badge" :class="h.voice_id ? 'ok' : 'err'">🎤 {{ h.voice_name }}</span>
        </div>
        <div class="sub" style="margin-top:4px;">{{ h.desc || '暂无描述' }}</div>
      </div>
      <button class="btn small danger" @click.stop="deleteHuman(h)">删除</button>
    </div>

    <!-- 数字人详情 -->
    <template v-if="detail">
      <div class="card">
        <div class="tabs" style="margin-bottom:16px;">
          <div class="tab" :class="{ active: activeTab === 'overview' }" @click="activeTab = 'overview'">概况</div>
          <div class="tab" :class="{ active: activeTab === 'data' }" @click="activeTab = 'data'">训练数据</div>
          <div class="tab" :class="{ active: activeTab === 'training' }" @click="activeTab = 'training'">训练配置</div>
          <div class="tab" :class="{ active: activeTab === 'inference' }" @click="activeTab = 'inference'">推理参数</div>
          <div class="tab" :class="{ active: activeTab === 'polish' }" @click="activeTab = 'polish'">润色设置</div>
        </div>

        <!-- 概况 -->
        <div v-if="activeTab === 'overview'">
          <div class="row" style="gap:10px; margin-bottom:12px;">
            <label class="field grow"><span>名称</span>
              <input v-model="detail.name" type="text" />
            </label>
            <label class="field grow"><span>绑定声音模型</span>
              <select v-model="detail.voice_id">
                <option value="">未绑定</option>
                <option v-for="v in store.voices" :key="v.id" :value="v.id">{{ v.name }}（{{ v.source }}）</option>
              </select>
            </label>
          </div>
          <label class="field"><span>描述</span>
            <textarea v-model="detail.desc" rows="2" placeholder="这个数字人的特点..."></textarea>
          </label>
          <div class="row" style="margin-top:12px;">
            <button class="btn primary" @click="updateHuman">💾 保存</button>
          </div>

          <!-- 训练数据质量 -->
          <div v-if="detail.data_stats && !detail.data_stats.error" style="margin-top:16px;">
            <h4>📊 训练数据质量</h4>
            <div class="row" style="gap:12px; margin-bottom:10px;">
              <span class="badge" :class="qualityColor(detail.data_stats.quality_score)">
                得分 {{ detail.data_stats.quality_score }} {{ qualityLabel(detail.data_stats.quality_score) }}
              </span>
              <span class="badge accent">{{ detail.data_stats.total_minutes }} 分钟 / {{ detail.data_stats.total_files }} 文件</span>
              <span class="badge">{{ detail.data_stats.sentences }} 句</span>
            </div>
            <div v-if="detail.data_stats.issues?.length" style="margin-top:8px;">
              <div v-for="(issue, i) in detail.data_stats.issues" :key="i" class="muted" style="font-size:12px;">
                {{ issue }}
              </div>
            </div>
            <div v-if="detail.data_stats.content_types" class="row" style="gap:8px; margin-top:8px;">
              <span v-for="(count, type) in detail.data_stats.content_types" :key="type" class="badge">
                {{ { prose: '散文', poetry: '诗歌', fiction: '小说', classical: '古文', other: '其他' }[type] || type }}: {{ count }}句
              </span>
            </div>
          </div>
        </div>

        <!-- 训练数据 -->
        <div v-if="activeTab === 'data'">
          <!-- 数据源切换：主数据 / s1 语感 / s2 音色 -->
          <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap;">
            <div class="tab" :class="{ active: dataSource === 'main' }" @click="switchDataSource('main')">📦 主数据</div>
            <div class="tab" :class="{ active: dataSource === 's1' }" @click="switchDataSource('s1')">🗣 s1 语感数据</div>
            <div class="tab" :class="{ active: dataSource === 's2' }" @click="switchDataSource('s2')">🎤 s2 音色数据</div>
          </div>
          <div class="hint" v-if="dataSource === 'main'">
            <b>主数据（默认训练数据）：</b>不单独指定 s1/s2 时，音色和语感都用这份数据训练（混合训练）。<br>
            <b>训练数据准备流程：</b>① 设置目录 → ② 上传长音频 → ③ 系统自动切片+ASR标注 → ④ <b>人工逐条校对文字</b>（致命点！错字=废训练）→ ⑤ 训练
          </div>
          <div class="hint" v-else>
            <b>{{ dataSourceLabel[dataSource] }}（独立数据目录）：</b>上传自己的音频分别训练——s1 决定<b>语感/韵律/停顿</b>，s2 决定<b>音色/声线</b>。
            上传后自动创建独立目录并切片+标注；训练时 s1 用 s1 数据、s2 用 s2 数据（未上传则回退主数据）。
          </div>

          <!-- 目录设置 -->
          <template v-if="dataSource === 'main'">
            <label class="field"><span>训练数据目录路径</span>
              <input v-model="detail.data_dir" type="text" placeholder="F:\my_recordings" />
            </label>
            <button class="btn primary" @click="updateHuman" style="margin-bottom:12px;">💾 保存目录</button>
            <button class="btn" @click="loadTrainingData()" :disabled="!detail.data_dir" style="margin-bottom:12px; margin-left:8px;">📂 加载数据</button>
          </template>
          <!-- 切分方式：与「音频分片」模块同一套实现 -->
          <div class="row" style="gap:10px; align-items:flex-end; margin-bottom:10px; flex-wrap:wrap;">
            <label>切分方式
              <select v-model="sliceOpts.mode">
                <option value="silence">按静音（快，可能腰斩句子）</option>
                <option value="align">识别对齐（句子完整，自带标注）</option>
              </select>
            </label>
            <label v-if="sliceOpts.mode === 'align'">粒度
              <select v-model="sliceOpts.granularity">
                <option value="para">按段（适合 s1 语感）</option>
                <option value="sentence">按句（适合 s2 音色）</option>
              </select>
            </label>
            <span v-if="sliceOpts.mode === 'align'" class="hint" style="font-size:11px;">
              先把整条音频识别一次（慢一些），刀口只落在标点之后，标注随识别到位、不再二次 ASR。
            </span>
          </div>
          <div v-if="sliceOpts.mode === 'align'" style="margin-bottom:10px;">
            <label style="font-size:12px;">热词（可选，空格或逗号分隔）—— 专名老被同音写错时才填
              <textarea v-model="sliceOpts.hotwords" rows="2"
                        style="width:100%; box-sizing:border-box; font-size:12px;"
                        placeholder="例如：姚氏 岳和 岳飞 王员外 黑风岭 汤阴县 片瓦无存"></textarea>
            </label>
            <div class="muted" style="font-size:11px; margin-top:2px;">
              填了会改用支持热词偏置的 SeACo-Paraformer 重新识别（改了热词会重跑识别，不改则复用缓存）。
              实测在评书素材上：「岳河→岳和」「片着瓦无存→片瓦无存」被纠正，但同一次里「王员外」被带成了
              「王外外」—— 热词会偏置整个解码，切完抽查专名附近那几段。
            </div>
          </div>
          <!-- s1/s2 目录设置：可留空自动创建，也可自定义路径后直接放文件 -->
          <template v-if="dataSource === 's1' || dataSource === 's2'">
            <label class="field"><span>{{ dataSourceLabel[dataSource] }}目录路径</span>
              <input v-model="detail[dataSource === 's1' ? 's1_data_dir' : 's2_data_dir']" type="text" :placeholder="detail.data_dir ? detail.data_dir + '_' + dataSource : 'F:\\my_' + dataSource + '_audio（留空=上传时自动创建）'" />
            </label>
            <button class="btn primary" @click="updateHuman" style="margin-bottom:12px;">💾 保存目录</button>
            <button class="btn" @click="processDir" :disabled="uploadBusy" style="margin-bottom:12px; margin-left:8px;">🔧 处理目录音频（切片+ASR）</button>
            <button class="btn" @click="loadTrainingData()" style="margin-bottom:12px; margin-left:8px;">📂 加载数据</button>
            <div class="hint">留空即可：上传音频时自动创建独立目录；也可手动填路径（把音频文件直接放进该目录后点「处理目录音频」）。</div>
          </template>

          <!-- 上传长音频 -->
          <div class="card" :style="(dataSource !== 'main' || detail.data_dir) ? 'border-left:3px solid var(--accent)' : 'border-left:3px solid var(--warn)'">
            <h4>📤 上传长音频（{{ dataSourceLabel[dataSource] }}）</h4>
            <div v-if="dataSource === 'main' && !detail.data_dir" class="hint">⚠️ 请先填写上方目录路径并保存</div>
            <div v-else-if="dataSource === 's1'" class="hint">上传<b>语感素材</b>（如你自己的朗读），系统自动切片+ASR标注。训练时 s1 用它学韵律/停顿/语感。</div>
            <div v-else-if="dataSource === 's2'" class="hint">上传<b>音色素材</b>（如 Edge 磁性音频），系统自动切片+ASR标注。训练时 s2 用它学音色/声线。</div>
            <div v-else class="hint">上传完整朗读录音（5分钟~1小时），系统自动切片+ASR标注。</div>
            <label class="btn primary" :style="{ cursor: (dataSource === 'main' && !detail.data_dir) ? 'not-allowed' : 'pointer', opacity: (dataSource === 'main' && !detail.data_dir) ? 0.5 : 1 }">
              🎙️ 上传长音频或压缩包（wav/mp3/m4a/zip/rar）
              <input type="file" accept=".wav,.mp3,.m4a,.flac,.aac,.ogg,.zip,.rar,.7z" style="display:none;" @change="uploadAudio" :disabled="uploadBusy" />
            </label>
            <span v-if="uploadBusy" class="muted" style="margin-left:12px;">切片+标注中…</span>
          </div>

          <!-- 文本校对界面 -->
          <div v-if="dataTotal > 0" style="margin-top:16px;">
            <h4>📝 文本校对（{{ dataTotal }} 条）</h4>
            <div class="hint" style="border-left:3px solid var(--err); padding-left:10px;">
              ⚠️ <b>这是训练成败的关键步骤！</b><br>
              每条音频对应的文本必须一字不差。逐条检查：听音频 → 读文字 → 发现错字就修正 → 保存。漏校=训练质量下降。
            </div>
            <div v-for="item in trainingData" :key="item.idx" class="list-item" style="flex-direction:column; align-items:stretch; gap:6px;">
              <div class="row" style="gap:8px; font-size:12px;">
                <span class="badge accent">#{{ item.idx + 1 }}</span>
                <span class="muted mono" style="font-size:11px; flex:1;">{{ item.audio }}</span>
                <span class="badge" :class="item.text && item.saved ? 'ok' : 'err'">
                  {{ item.text && item.saved ? '✅ 已标注' : item.text ? '⏳ 保存中…' : '待标注' }}
                </span>
                <span v-if="item.flags" class="badge" title="刀口没等到句末标点或引号未闭合，建议先听这一段"
                      style="color:#d97706;">⚠ 边界可疑</span>
              </div>
              <!-- 在线播放 -->
              <audio v-if="item.audio_url" :src="item.audio_url" controls style="width:100%; height:32px;"></audio>
              <div class="row" style="gap:6px;">
                <input v-model="item.text" type="text" placeholder="听音频内容，逐字输入（自动保存）…" style="flex:1;"
                  @keyup.enter="saveText(item)" @input="onTextInput(item)" :class="{ 'err-text': !item.text }" />
                <button class="btn small success" @click="saveText(item)">💾 保存</button>
                <button class="btn small danger" title="删除此切片（杂音/无效）" @click="deleteSlice(item)">🗑️</button>
              </div>
            </div>
            <!-- 分页控件 -->
            <div v-if="dataPages > 1" class="row" style="justify-content:center; gap:8px; margin-top:14px;">
              <button class="btn small" :disabled="dataPage <= 1" @click="loadTrainingData(dataPage - 1)">← 上一页</button>
              <select v-model.number="dataSize" @change="loadTrainingData(1)" style="width:80px;">
                <option :value="10">10 条/页</option>
                <option :value="20">20 条/页</option>
                <option :value="50">50 条/页</option>
              </select>
              <span class="muted" style="font-size:12px;">第 {{ dataPage }} / {{ dataPages }} 页</span>
              <button class="btn small" :disabled="dataPage >= dataPages" @click="loadTrainingData(dataPage + 1)">下一页 →</button>
            </div>
          </div>
        </div>

        <!-- 训练配置 -->
        <div v-if="activeTab === 'training'">
          <div class="hint">
            <b>8GB GPU 专属参数（千问方案）：</b><br>
            SoVITS: batch=4, epochs=10-15, lr=1e-4, FP16<br>
            GPT: batch=4, epochs=10-15, lr=1e-4, DPO=OFF<br>
            ⚠️ epoch 不要超过20！过拟合=杂音卡顿。
          </div>
          <div class="row" style="gap:12px; margin-bottom:12px;">
            <button class="btn primary" @click="detail.training_config = { sovits: { batch_size: 4, epochs: 15, lr: 0.0001, fp16: true }, gpt: { batch_size: 4, epochs: 15, lr: 0.0001, dpo: false } }">⚙️ 应用8GB优化参数</button>
            <button class="btn" @click="detail.training_config = { sovits: { batch_size: 8, epochs: 20, lr: 0.0001, fp16: true }, gpt: { batch_size: 8, epochs: 25, lr: 0.0001, dpo: false } }">⚙️ 应用16GB参数</button>
          </div>
          <div v-if="detail.training_config && detail.training_config.sovits && detail.training_config.gpt" style="margin-top:12px;">
            <h4>当前训练参数</h4>
            <div class="row" style="gap:12px;">
              <div class="card" style="flex:1; margin:0;">
                <b>SoVITS（音色）</b>
                <label class="field" style="margin:4px 0;"><span>训练轮次 epochs</span>
                  <input type="number" v-model.number="detail.training_config.sovits.epochs" min="2" max="100" />
                </label>
                <label class="field" style="margin:4px 0;"><span>batch_size</span>
                  <input type="number" v-model.number="detail.training_config.sovits.batch_size" min="1" max="32" />
                </label>
                <label class="field" style="margin:4px 0;"><span>学习率 lr</span>
                  <input type="number" v-model.number="detail.training_config.sovits.lr" min="0.00001" max="0.01" step="0.00001" />
                </label>
                <label class="field" style="margin:4px 0;">
                  <input type="checkbox" v-model="detail.training_config.sovits.fp16" /> FP16 混合精度
                </label>
              </div>
              <div class="card" style="flex:1; margin:0;">
                <b>GPT（语感/节奏）</b>
                <label class="field" style="margin:4px 0;"><span>训练轮次 epochs</span>
                  <input type="number" v-model.number="detail.training_config.gpt.epochs" min="2" max="100" />
                </label>
                <label class="field" style="margin:4px 0;"><span>batch_size</span>
                  <input type="number" v-model.number="detail.training_config.gpt.batch_size" min="1" max="32" />
                </label>
                <label class="field" style="margin:4px 0;"><span>学习率 lr</span>
                  <input type="number" v-model.number="detail.training_config.gpt.lr" min="0.00001" max="0.01" step="0.00001" />
                </label>
                <label class="field" style="margin:4px 0;">
                  <input type="checkbox" v-model="detail.training_config.gpt.dpo" /> DPO（防过拟合）
                </label>
              </div>
            </div>
          </div>
          <!-- 基础模型选择（迭代训练） -->
          <div v-if="baseModels.length > 1" style="margin-top:12px;">
            <label class="field"><span>基础模型（迭代训练）</span>
              <select v-model="selectedBaseModel" style="width:100%;">
                <option value="">官方预训练（从零开始）</option>
                <option v-for="m in baseModels.filter(m => m.type === 'trained')" :key="m.id" :value="m.id">
                  {{ m.name }}（{{ m.duration ? Math.floor(m.duration / 60) + '分钟' : '' }}）
                </option>
              </select>
            </label>
            <div class="hint" v-if="selectedBaseModel">✅ 基于所选模型继续微调（迁移学习），效果更好。可选其他数字人的已训练模型。</div>
          </div>
          <!-- 数据源状态（s1/s2 独立数据预览） -->
          <div style="margin-top:12px; font-size:12px;">
            <div class="hint" style="border-left:3px solid var(--accent); padding-left:10px;">
              <b>🎛 数据源组合：</b>训练时 <b>s1（语感）</b>用「{{ dataSourceLabel.s1 }}」的数据、<b>s2（音色）</b>用「{{ dataSourceLabel.s2 }}」的数据；
              某源未上传时自动回退主数据。<br>
              想混合「自己的语感 + Edge 音色」：在「训练数据」标签页的 s1 区传自己的朗读、s2 区传 Edge 音频，然后直接开始训练。
            </div>
          </div>
          <div class="row" style="margin-top:12px;">
            <button class="btn primary" @click="updateHuman">💾 保存配置</button>
            <button class="btn success" :disabled="trainBusy || detail.status === 'training'" @click="startTraining">
              🚀 开始训练（{{ detail.status === 'training' ? '训练中…' : trainBusy ? '准备中…' : '一键启动' }}）
            </button>
          </div>
          <template v-if="trainJob">
            <div class="progress"><div :style="{ width: (trainJob.progress || 0) + '%' }"></div></div>
            <div class="job-line">⚙️ {{ trainJob.message }}</div>
            <!-- 实时训练日志 -->
            <div v-if="trainLogs.length" style="margin-top:10px; background:var(--card2); border-radius:8px; padding:10px; max-height:260px; overflow-y:auto; font-family:monospace; font-size:11px; line-height:1.6; border:1px solid var(--border);">
              <div v-for="(log, i) in trainLogs.slice(-40)" :key="i" style="white-space:pre-wrap; word-break:break-all;" :style="{ color: log.includes('错误') || log.includes('Error') ? 'var(--err)' : log.includes('完成') ? 'var(--ok)' : 'var(--muted)' }">
                {{ log }}
              </div>
              <div v-if="!trainLogs.length" class="muted">正在启动训练，等待日志输出…</div>
            </div>
          </template>

          <!-- 训练历史 -->
          <div v-if="trainHistory.length" style="margin-top:16px;">
            <h4>📋 训练历史</h4>
            <div v-for="r in trainHistory" :key="r.id" class="list-item" style="padding:8px 12px; margin-bottom:4px;">
              <div style="flex:1;">
                <div class="row" style="gap:8px; font-size:12px;">
                  <span class="badge" :class="r.status === 'success' ? 'ok' : r.status === 'error' ? 'err' : 'warn'">
                    {{ { success: '✓ 成功', error: '✗ 失败', running: '⏳ 进行中' }[r.status] || r.status }}
                  </span>
                  <span class="muted">{{ r.created_at?.slice(0, 16).replace('T', ' ') }}</span>
                  <span v-if="r.duration_sec" class="muted">耗时 {{ Math.floor(r.duration_sec / 60) }}分钟</span>
                </div>
                <div class="muted" style="font-size:11px; margin-top:4px;">
                  数据: {{ r.data_file_count }}个文件 / {{ (r.data_duration / 60).toFixed(0) }}分钟
                  · 模型: {{ r.model_s2 ? 'S2 ✓' : '-' }} + {{ r.model_gpt ? 'GPT ✓' : '-' }}
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 推理参数 -->
        <div v-if="activeTab === 'inference'">
          <div class="hint">
            <b>推荐参数（三家方案综合）：</b><br>
            文学朗读：temperature=0.72, top_p=0.85, top_k=15（豆包+千问推荐）<br>
            稳定朗读：temperature=0.6, top_p=0.8, top_k=10<br>
            生动演绎：temperature=0.85, top_p=0.9, top_k=20
          </div>
          <div class="row" style="gap:8px; margin-bottom:16px;">
            <button class="btn" v-for="(p, name) in presets" :key="name" @click="detail.inference_params = { ...p }; updateHuman(); toast('已应用 ' + name + ' 参数')">
              📎 {{ name }}
            </button>
          </div>
          <div class="slider-grid" v-if="detail.inference_params">
            <div class="slider-item">
              <label>温度（创造力）<b>{{ (detail.inference_params.temperature || 0.72).toFixed(2) }}</b></label>
              <input type="range" min="0.1" max="1.5" step="0.01" v-model.number="detail.inference_params.temperature" />
              <span class="muted" style="font-size:10px;">越高越有感情，越低越稳定；文学推荐0.72</span>
            </div>
            <div class="slider-item">
              <label>Top-P（核采样）<b>{{ (detail.inference_params.top_p || 0.85).toFixed(2) }}</b></label>
              <input type="range" min="0.1" max="1.0" step="0.01" v-model.number="detail.inference_params.top_p" />
            </div>
            <div class="slider-item">
              <label>Top-K <b>{{ detail.inference_params.top_k || 15 }}</b></label>
              <input type="range" min="1" max="50" step="1" v-model.number="detail.inference_params.top_k" />
            </div>
            <div class="slider-item">
              <label>重复惩罚 <b>{{ (detail.inference_params.repetition_penalty || 1.05).toFixed(2) }}</b></label>
              <input type="range" min="1.0" max="1.5" step="0.01" v-model.number="detail.inference_params.repetition_penalty" />
            </div>
          </div>
          <div class="row" style="margin-top:12px;">
            <button class="btn primary" @click="updateHuman">💾 保存</button>
          </div>
        </div>

        <!-- 润色设置 -->
        <div v-if="activeTab === 'polish'">
          <div class="hint">
            绑定润色参数：数字人朗读的音频会自动经过这套 EQ/混响/压缩处理，让声音更饱满更有磁性。
          </div>
          <label class="field"><span>润色预设</span>
            <select v-model="selectedPreset" @change="applyPolishPreset(selectedPreset)">
              <option value="">自定义（手动调整参数）</option>
              <option value="饱满磁性">饱满磁性（低频+4/压缩+混响）</option>
              <option value="清晰明亮">清晰明亮（中频+2/高频+3）</option>
              <option value="柔和温暖">柔和温暖（低频+3/高通100）</option>
            </select>
          </label>
          <button class="btn primary" @click="updateHuman">💾 保存</button>
        </div>
      </div>
    </template>

    <!-- 创建数字人弹窗 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal">
        <h3>创建新数字人</h3>
        <label class="field"><span>数字人名称</span>
          <input v-model="form.name" type="text" placeholder="例：我的朗读声音" />
        </label>
        <label class="field"><span>绑定声音模型（可选，之后随时绑定）</span>
          <select v-model="form.voice_id">
            <option value="">不绑定（稍后设置）</option>
            <option v-for="v in store.voices" :key="v.id" :value="v.id">{{ v.name }}（{{ v.source }}）</option>
          </select>
        </label>
        <label class="field"><span>描述</span>
          <textarea v-model="form.desc" rows="2" placeholder="这个数字人的特点和用途..."></textarea>
        </label>
        <div class="row" style="justify-content:flex-end;">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn primary" @click="createHuman">创建</button>
        </div>
      </div>
    </div>
  </div>
</template>
