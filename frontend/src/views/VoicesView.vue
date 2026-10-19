<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { store, refreshAll, pollJob, engineInfo } from '../store.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'

const showCreate = ref(false)
const busy = reactive({})
const form = reactive({
  name: '',
  source: 'edge',
  edge_voice: 'zh-CN-XiaoxiaoNeural',
  ref_text: '',
  ref_lang: 'zh',
  timbre: '',
})
const refFile = ref(null)
const editing = ref(null) // 编辑中的声音 id

const engineOptions = computed(() =>
  store.engines.map((e) => ({ id: e.id, name: e.name, ok: e.ok, detail: e.check_message, hint: e.cost_hint, clone: e.supports_clone }))
)

const presetVoices = computed(() => {
  const eng = store.engines.find((e) => e.id === form.source)
  return eng?.preset_voices || []
})

function openCreate() {
  Object.assign(form, { name: '', source: 'edge', edge_voice: 'zh-CN-XiaoxiaoNeural', ref_text: '', ref_lang: 'zh', timbre: '' })
  refFile.value = null
  editing.value = null
  showCreate.value = true
}

function openEdit(v) {
  Object.assign(form, {
    name: v.name, source: v.source, edge_voice: v.edge_voice, ref_text: v.ref_text, ref_lang: v.ref_lang, timbre: v.timbre,
  })
  refFile.value = null
  editing.value = v.id
  showCreate.value = true
}

async function submitCreate() {
  try {
    const body = {
      name: form.name || '未命名声音',
      source: form.source,
      edge_voice: form.edge_voice,
      ref_text: form.ref_text,
      ref_lang: form.ref_lang,
      timbre: form.timbre,
    }
    let v
    if (editing.value) {
      v = await api.updateVoice(editing.value, body)
    } else {
      v = await api.createVoice(body)
    }
    if (refFile.value) {
      await api.uploadRefAudio(v.id, refFile.value)
    }
    await refreshAll()
    showCreate.value = false
    toast(editing.value ? '已保存' : '已创建声音，可上传参考音频并克隆')
  } catch (e) {
    errToast(e)
  }
}

function onFile(e) {
  refFile.value = e.target.files[0] || null
}

async function uploadRef(v, e) {
  const f = e.target.files?.[0]
  if (!f) return
  try {
    busy[v.id] = '上传参考音频…'
    await api.uploadRefAudio(v.id, f)
    await refreshAll()
    toast(`参考音频已上传（${v.name}），可执行克隆`)
  } catch (err) {
    errToast(err)
  } finally {
    busy[v.id] = ''
  }
}

async function clone(v) {
  try {
    if (v.source === 'edge') {
      await api.cloneVoice(v.id)
      await refreshAll()
      toast('Edge 为云端音色，已启用；每次合成都会联网调用微软服务，如需离线请用 GPT-SoVITS 克隆')
      return
    }
    const r = await api.cloneVoice(v.id)
    if (r.job_id) {
      busy[v.id] = '克隆中…'
      pollJob(r.job_id, async (j) => {
        if (j.status === 'success') {
          busy[v.id] = ''
          toast(`「${v.name}」克隆完成`)
          await refreshAll()
        } else if (j.status === 'error') {
          busy[v.id] = ''
          errToast(`克隆失败：${j.message}`)
          await refreshAll()
        }
      })
    }
  } catch (e) {
    errToast(e)
  }
}

async function removeVoice(v) {
  if (!confirm(`确定删除声音「${v.name}」？相关文件将被移除。`)) return
  try {
    await api.deleteVoice(v.id)
    await refreshAll()
    toast('已删除')
  } catch (e) {
    errToast(e)
  }
}

const statusBadge = (s) =>
  ({ draft: '未就绪', ready: '就绪', training: '克隆中', error: '失败' }[s] || s)

const statusTone = (s) =>
  ({ ready: 'ok', training: 'warn', error: 'err', draft: '' }[s] || '')

const srcName = (s) => ({ edge: 'Edge 免费', gpt_sovits: 'GPT-SoVITS', minimax: 'MiniMax', fish: 'Fish Audio', openvino: 'OpenVINO' }[s] || s)

// ---------- 微调训练 ----------
const trainBusy = ref('')           // 训练中的声音 id
const trainJob = ref(null)
const trainEpochs = reactive({ s2: 10, s1: 10 })
const trainDataInfo = ref({})       // {voice_id: {files, duration}}
const auxSel = ref({})              // {voice_id: [aux voice ids]}

async function uploadTrain(v, e) {
  const files = Array.from(e.target.files || [])
  if (!files.length) return
  try {
    let total = { files: 0, duration: 0 }
    for (const f of files) {
      const r = await api.uploadTrainAudio(v.id, f)
      total.files += r.files
      total.duration += r.duration
    }
    trainDataInfo.value[v.id] = total
    toast(`训练数据已上传：${total.files} 个音频，约 ${Math.round(total.duration)} 秒`)
  } catch (err) { errToast(err) }
  e.target.value = ''
}

async function startTrain(v) {
  if (v.source !== 'gpt_sovits') { toast('仅 GPT-SoVITS 声音支持微调训练', 'error'); return }
  trainBusy.value = v.id
  try {
    const r = await api.trainVoice(v.id, { speaker: v.name || 'speaker0', s2_epochs: trainEpochs.s2, s1_epochs: trainEpochs.s1 })
    trainJob.value = { id: r.job_id, progress: 0, message: '排队中…' }
    pollJob(r.job_id, async (j) => {
      trainJob.value = { progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        trainBusy.value = ''
        toast(`「${v.name}」微调训练完成！声音模型已就绪，可自由合成任意文本`)
        await refreshAll()
      } else if (j.status === 'error') {
        trainBusy.value = ''
        errToast(`训练失败：${j.message}`)
      }
    })
  } catch (e) { trainBusy.value = ''; errToast(e) }
}

async function saveAuxMix(v) {
  try {
    const cfg = { ...(v.engine_config || {}) }
    cfg.aux_refs = auxSel.value[v.id] || []
    await api.updateVoice(v.id, { engine_config: cfg })
    v.engine_config = cfg
    toast(`已保存混合音色配置（${cfg.aux_refs.length} 个辅助参考）`)
  } catch (e) { errToast(e) }
}

function initAuxSel(v) {
  if (!(v.id in auxSel.value)) {
    auxSel.value[v.id] = [...(v.engine_config?.aux_refs || [])]
  }
}

const isFinetuned = (v) => !!(v.engine_config && v.engine_config.finetuned)

// 渲染时初始化混合音色选择状态
watch(() => store.voices.map((v) => v.id), () => {
  store.voices.forEach((v) => initAuxSel(v))
}, { immediate: true })
</script>

<template>
  <div>
    <div class="page-title">🎙️ 声音库</div>
    <div class="page-sub">克隆自己、家人、朋友的声音，微调润色，形成具有独特音色的「声音模型」。参考音频建议 10~60 秒清晰人声。</div>

    <!-- 引擎状态 -->
    <div class="card">
      <h3>🔌 引擎状态</h3>
      <div class="row">
        <div v-for="e in engineOptions" :key="e.id" class="card" style="flex:1; min-width:220px; margin:0;">
          <div class="row" style="justify-content:space-between;">
            <b>{{ e.name }}</b>
            <span class="badge" :class="e.ok ? 'ok' : 'err'">{{ e.ok ? '可用' : '未配置' }}</span>
          </div>
          <div class="muted" style="font-size:12px; margin-top:6px;">{{ e.detail }}</div>
          <div class="muted" style="font-size:12px; margin-top:4px;">💰 {{ e.hint }}</div>
        </div>
      </div>
    </div>

    <div class="row" style="justify-content:space-between; margin-bottom:12px;">
      <h3 style="margin:0;">我的声音</h3>
      <button class="btn primary" @click="openCreate">＋ 克隆新声音</button>
    </div>

    <div v-if="!store.voices.length" class="empty">
      还没有声音。点击「克隆新声音」开始 —— 上传一段清晰人声，即可生成专属音色。
    </div>

    <div v-for="v in store.voices" :key="v.id" class="list-item">
      <div style="flex:1; min-width:0;">
        <div class="row" style="gap:8px;">
          <span class="name">{{ v.name }}</span>
          <span class="badge accent">{{ srcName(v.source) }}</span>
          <span class="badge" :class="statusTone(v.status)">{{ statusBadge(v.status) }}</span>
        </div>
        <div class="sub">
          音色：{{ v.timbre || '未描述' }}
          <template v-if="v.ref_audio"> · 参考音频 {{ v.duration ? v.duration.toFixed(1) + 's' : '已上传' }}</template>
          <template v-if="v.source === 'edge' && v.edge_voice"> · {{ presetVoices.find(p => p.id === v.edge_voice)?.name || v.edge_voice }}</template>
          <template v-if="v.source === 'edge'"> · <span style="color:#d97706;">云端音色，合成需联网（非本地克隆）</span></template>
          <template v-if="v.error"> · <span class="err-text">{{ v.error }}</span></template>
        </div>
      </div>
      <div class="actions">
        <span v-if="busy[v.id]" class="muted" style="font-size:12px;">{{ busy[v.id] }}</span>
        <button class="btn small" @click="openEdit(v)">编辑</button>
        <label class="btn small" :style="{ cursor: 'pointer' }">
          上传参考音频
          <input type="file" accept="audio/*" style="display:none" @change="(e) => uploadRef(v, e)" />
        </label>
        <button class="btn small success" :disabled="!v.ref_audio && v.source !== 'edge'" @click="clone(v)">
          {{ v.source === 'edge' ? '启用' : (v.status === 'ready' ? '重新克隆' : '克隆') }}
        </button>
        <button class="btn small danger" @click="removeVoice(v)">删除</button>
      </div>
    </div>

    <!-- GPT-SoVITS 微调训练面板 -->
    <div v-for="v in store.voices.filter((x) => x.source === 'gpt_sovits')" :key="'train-' + v.id" class="card" style="border-left:3px solid var(--accent);">
      <h3>🎓 「{{ v.name }}」微调训练
        <span class="badge" :class="isFinetuned(v) ? 'ok' : ''">{{ isFinetuned(v) ? '✓ 微调模型已就绪' : '零样本模式' }}</span>
        <span v-if="trainBusy === v.id" class="badge warn">训练中…</span>
      </h3>
      <div class="hint">
        零样本克隆只是「模仿」；微调训练才真正形成<b>声音模型</b>：采集你的音色特征训练专属模型，
        之后任意长度文本都能稳定像真人朗读。<b>想混入名人音色形成独特声音</b>：把名人的公开音频也放进训练数据（或配置下方混合音色），
        模型会自动融合多种音色特征。
      </div>
      <div class="row" style="margin-bottom:8px;">
        <label class="btn small" style="cursor:pointer;">
          📤 上传训练音频（可多选/zip：你自己的朗读 1~3 分钟最佳，可混入名人素材）
          <input type="file" accept="audio/*,.zip" multiple style="display:none" @change="(e) => uploadTrain(v, e)" />
        </label>
        <span v-if="trainDataInfo[v.id]" class="badge ok">
          已上传 {{ trainDataInfo[v.id].files }} 个 / {{ Math.round(trainDataInfo[v.id].duration) }} 秒
        </span>
        <span v-else class="muted" style="font-size:12px;">未上传训练数据</span>
      </div>
      <div class="row">
        <label class="field" style="margin:0;"><span>音色训练轮次</span>
          <select v-model.number="trainEpochs.s2" style="width:110px;">
            <option :value="5">快速（5）</option>
            <option :value="10">标准（10）</option>
            <option :value="20">精细（20）</option>
          </select>
        </label>
        <label class="field" style="margin:0;"><span>语义训练轮次</span>
          <select v-model.number="trainEpochs.s1" style="width:110px;">
            <option :value="5">快速（5）</option>
            <option :value="10">标准（10）</option>
            <option :value="20">精细（20）</option>
          </select>
        </label>
        <button class="btn primary" :disabled="!!trainBusy || isFinetuned(v)" @click="startTrain(v)">🎓 发起微调训练</button>
        <span class="muted" style="font-size:11px;">首次训练约 20~60 分钟（含 ASR 模型下载），期间请勿关闭服务</span>
      </div>
      <template v-if="trainJob && trainBusy === v.id">
        <div class="progress"><div :style="{ width: (trainJob.progress || 0) + '%' }"></div></div>
        <div class="job-line">⚙️ {{ trainJob.message }}</div>
      </template>

      <!-- 混合音色（推理级融合） -->
      <div style="margin-top:10px; border-top:1px dashed var(--border); padding-top:10px;">
        <div class="muted" style="font-size:12px; margin-bottom:6px;">
          🎨 混合音色（无需训练即可试听）：选择其他克隆声音作为辅助参考，合成时自动融合双方音色形成独特声音
        </div>
        <div class="row" style="gap:8px;">
          <label v-for="ov in store.voices.filter((x) => x.source === 'gpt_sovits' && x.id !== v.id && x.status === 'ready')" :key="ov.id"
            class="row" style="gap:6px; cursor:pointer; padding:4px 10px; border:1px solid var(--border); border-radius:16px;"
            :style="auxSel[v.id]?.includes(ov.id) ? { borderColor: 'var(--accent)', background: 'rgba(108,140,255,.12)' } : {}">
            <input type="checkbox" :value="ov.id" v-model="auxSel[v.id]" style="accent-color:var(--accent);" @change="saveAuxMix(v)" />
            {{ ov.name }}
          </label>
          <span v-if="!store.voices.filter((x) => x.source === 'gpt_sovits' && x.id !== v.id && x.status === 'ready').length" class="muted" style="font-size:12px;">
            还没有其他已克隆的 GPT-SoVITS 声音（先克隆一个「名人音色」即可在此混合）
          </span>
        </div>
      </div>
    </div>

    <!-- 创建/编辑弹窗 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal">
        <h3>{{ editing ? '编辑声音' : '克隆新声音' }}</h3>
        <label class="field"><span>声音名称（如「我的声音」「爸爸」「晓明」）</span>
          <input v-model="form.name" type="text" placeholder="例：我的声音" />
        </label>
        <label class="field"><span>引擎（决定克隆方式与费用）</span>
          <select v-model="form.source">
            <option v-for="e in engineOptions" :key="e.id" :value="e.id" :disabled="e.id !== 'edge' && !e.ok">
              {{ e.name }} — {{ e.hint }}（{{ e.ok ? '可用' : e.detail }}）
            </option>
          </select>
        </label>

        <template v-if="form.source === 'edge'">
          <label class="field"><span>选择固定音色（免费，无需克隆）</span>
            <select v-model="form.edge_voice">
              <option v-for="p in presetVoices" :key="p.id" :value="p.id">{{ p.name }}（{{ p.lang }}）</option>
            </select>
          </label>
        </template>
        <template v-else>
          <label class="field"><span>参考音频（10~60 秒清晰人声，格式 mp3/wav 等）</span>
            <input type="file" accept="audio/*" @change="onFile" />
          </label>
          <label class="field"><span>参考音频对应文本（克隆质量关键：必须与音频内容一致）</span>
            <textarea v-model="form.ref_text" rows="2" placeholder="例：大家好，欢迎收听我的声音。"></textarea>
          </label>
          <label class="field"><span>参考音频语言</span>
            <select v-model="form.ref_lang">
              <option value="zh">中文</option>
              <option value="en">英文</option>
              <option value="ja">日文</option>
              <option value="yue">粤语</option>
              <option value="auto">自动</option>
            </select>
          </label>
        </template>

        <label class="field"><span>音色描述（可选，方便记忆）</span>
          <input v-model="form.timbre" type="text" placeholder="例：低沉磁性男声 / 温柔女声" />
        </label>

        <div class="row" style="justify-content:flex-end;">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn primary" @click="submitCreate">{{ editing ? '保存' : '创建' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
