<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { pollJob } from '../store.js'

const tasks = ref([])
const cur = ref(null)          // 当前任务详情
const slices = ref([])
const page = ref(1)
const pages = ref(1)
const total = ref(0)
const size = 50
const filter = ref('all')        // all | unlabeled | deleted
const q = ref('')

const file = ref(null)
const busy = ref(false)
const job = ref(null)
const jobId = ref('')
const drafting = reactive({})   // slice_id -> 正在编辑的文本
const splitAt = reactive({})    // slice_id -> 拆分秒数输入

const params = reactive({ noise_db: -35, min_silence: 0.5, min_piece: 1.0, max_piece: 10.0, merge_gap: 3.0 })

// 三档预设：数值来自 180 秒真实评书音频的参数扫描（覆盖率均为 100%）
const PRESETS = {
  train: { label: '训练素材（3~10 秒）', p: { noise_db: -35, min_silence: 0.5, min_piece: 1.0, max_piece: 10, merge_gap: 3.0 } },
  sent: { label: '句子级（便于校对）', p: { noise_db: -35, min_silence: 0.5, min_piece: 0.8, max_piece: 15, merge_gap: 1.5 } },
  ref: { label: 'TTS 参考音频（6~10 秒）', p: { noise_db: -35, min_silence: 0.5, min_piece: 2.0, max_piece: 10, merge_gap: 5.0 } },
}
function applyPreset(k) {
  const s = PRESETS[k]
  if (!s) return
  Object.assign(params, s.p)
  toast(`已套用「${s.label}」`)
}
const labeledPct = computed(() => {
  if (!cur.value?.slice_count) return 0
  return Math.round((cur.value.labeled_count || 0) / cur.value.slice_count * 100)
})

onMounted(async () => { await loadTasks() })

async function loadTasks(keepCur = true) {
  try {
    const r = await api.sliceTasks(1, 30, '')
    tasks.value = r.items || []
    if (keepCur && cur.value) {
      const hit = tasks.value.find((t) => t.id === cur.value.id)
      if (hit) await openTask(hit)
    }
  } catch (e) { tasks.value = [] }
}

function onPick(e) {
  const f = e.target.files?.[0]
  if (!f) return
  file.value = f
  toast(`已选择「${f.name}」，点「开始分片」执行切分与自动标注`)
  e.target.value = ''
}

async function start(redo = false) {
  if (!file.value && !cur.value) { toast('请先选择音频文件', 'error'); return }
  busy.value = true
  job.value = { progress: 0, message: '排队中…' }
  try {
    let id = cur.value?.id
    if (file.value) {
      const up = await api.sliceUpload(file.value)
      id = up.task_id
      file.value = null
    }
    if (!id) throw new Error('没有可分片的任务')
    const r = await api.sliceProcess(id, { ...params, redo })
    jobId.value = r.job_id
    await openTask({ id })
    pollJob(r.job_id, async (j) => {
      job.value = { progress: j.progress, message: j.message, status: j.status }
      if (j.status === 'success') {
        busy.value = false
        jobId.value = ''
        const n = j.result?.slices ?? 0
        toast(`分片完成：${n} 段，已自动标注 ${j.result?.labeled ?? 0} 段${j.result?.note ? '（' + j.result.note + '）' : ''}`)
        job.value = null
        await loadTasks()
        await loadSlices()
      } else if (j.status === 'error') {
        busy.value = false
        jobId.value = ''
        errToast(`分片失败：${j.message}`)
      }
    })
  } catch (e) {
    busy.value = false
    job.value = null
    errToast(e)
  }
}

async function openTask(t) {
  cur.value = await api.sliceTask(t.id)
  page.value = 1
  await loadSlices()
}

async function loadSlices() {
  if (!cur.value) return
  const r = await api.sliceList(cur.value.id, page.value, size, q.value,
    filter.value === 'unlabeled', filter.value === 'deleted')
  slices.value = r.items || []
  // 编辑框必须预填已识别文本：留空的话显示的是灰色占位符，看着像只读
  slices.value.forEach((s) => { drafting[s.id] = s.text || "" })
  Object.keys(drafting).forEach((k) => { if (!slices.value.some((s) => s.id === k)) delete drafting[k] })
  pages.value = r.pages || 1
  total.value = r.total || 0
  cur.value = await api.sliceTask(cur.value.id)
}

async function stopJob() {
  if (!jobId.value) return
  try {
    await api.cancelJob(jobId.value)
    toast('已请求停止：已切出的片段会保留入库（未标注的可点「重标空白」补）')
  } catch (e) { errToast(e) }
}

async function goto(p) { page.value = p; await loadSlices() }

async function saveText(s) {
  const v = drafting[s.id]
  if (v === undefined) return
  try {
    const r = await api.sliceUpdateText(s.id, v)
    s.text = r.text
    s.edited = r.edited
    toast('标注已保存')
  } catch (e) { errToast(e) }
}

async function del(s) {
  if (!confirm(`删除片段 #${s.idx + 1}？\n\n「${(s.text || s.name).slice(0, 40)}」\n\n只是从导出/打包里去掉，文件仍在，可恢复。`)) return
  try {
    await api.sliceDelete(s.id)
    await loadSlices()
    toast('片段已删除（可恢复）')
  } catch (e) { errToast(e) }
}

async function restore(s) {
  try {
    await api.sliceRestore(s.id)
    await loadSlices()
    toast('片段已恢复')
  } catch (e) { errToast(e) }
}

async function split(s) {
  const at = Number(splitAt[s.id])
  if (!at || at <= 0.05 || at >= s.duration - 0.05) {
    toast(`切点要填在 0.05 ~ ${(s.duration - 0.05).toFixed(2)} 秒之间`, 'error')
    return
  }
  try {
    const r = await api.sliceSplit(s.id, at, true)
    toast(`已拆成 2 段：${r.into.join(' + ')}`)
    await loadSlices()
  } catch (e) { errToast(e) }
}

async function retext(onlyEmpty) {
  try {
    const r = await api.sliceRetext(cur.value.id, onlyEmpty)
    toast(`重新标注完成：更新 ${r.updated} / ${r.total} 段${r.note ? '（' + r.note + '）' : ''}`)
    await loadSlices()
  } catch (e) { errToast(e) }
}

async function removeTask() {
  const st = await api.sliceStats(cur.value.id).catch(() => ({ mb: 0 }))
  if (!confirm(`删除任务「${cur.value.title}」？\n\n包含源音频与全部分片，约 ${st.mb} MB，不可恢复。`)) return
  try {
    const r = await api.sliceDeleteTask(cur.value.id)
    toast(`任务已删除，释放 ${r.freed_mb} MB`)
    cur.value = null
    slices.value = []
    await loadTasks(false)
  } catch (e) { errToast(e) }
}

const NEW_HUMAN = '__new__'
const humans = ref([])
const imp = reactive({ open: false, pick: '', name: '', source: 'main', mode: 'append', busy: false, res: null })

async function toggleImport() {
  imp.open = !imp.open
  if (!imp.open) return
  if (!humans.value.length) {
    try { humans.value = await api.humans() } catch (e) { errToast(e) }
  }
}

async function doImport() {
  const isNew = imp.pick === NEW_HUMAN
  if (isNew && !imp.name.trim()) return errToast({ message: '请填写新数字人名字' })
  if (!isNew && !imp.pick) return errToast({ message: '请选择目标数字人' })
  imp.busy = true
  try {
    imp.res = await api.sliceImport(cur.value.id, {
      human_id: isNew ? '' : imp.pick,
      human_name: isNew ? imp.name.trim() : '',
      source: imp.source,
      mode: imp.mode,
    })
    toast(imp.res.message)
  } catch (e) { errToast(e) } finally { imp.busy = false }
}

const fmt = (n) => (n || 0).toFixed(2)
</script>

<template>
  <h2>🔪 音频分片标注</h2>
  <p class="muted" style="font-size:13px;">
    上传一份长音频 → 按静音自动切成片段 → 自动识别文字 → 逐条校对 → 试听 / 打包下载 / 导出 train.list。
    与「数字人」训练数据流程同源但相互独立，产物存放在 <span class="mono">data/slicing/</span>。
  </p>

  <div class="card">
    <h3>① 上传与切分参数</h3>
    <div class="row" style="gap:10px; flex-wrap:wrap;">
      <label class="btn primary" style="cursor:pointer;">
        📂 选择音频（wav/mp3/m4a/flac/aac/ogg/mp4）
        <input type="file" accept="audio/*,video/mp4" style="display:none" @change="onPick" />
      </label>
      <span v-if="file" class="badge ok">{{ file.name }}</span>
      <span v-else-if="cur" class="muted" style="font-size:12px;">将重跑当前任务「{{ cur.title }}」</span>
    </div>
    <div class="row" style="gap:8px; margin-bottom:8px;">
      <span class="muted" style="font-size:12px; line-height:26px;">预设：</span>
      <button v-for="(v, k) in PRESETS" :key="k" class="btn small" @click="applyPreset(k)">{{ v.label }}</button>
    </div>
    <div class="row" style="gap:14px; margin-top:12px; flex-wrap:wrap; font-size:12px;">
      <label>静音阈值 <b>{{ params.noise_db }} dB</b>
        <input type="range" min="-60" max="-15" step="1" v-model.number="params.noise_db" />
      </label>
      <label>最短片段 <b>{{ params.min_piece }}s</b>
        <input type="range" min="0.3" max="5" step="0.1" v-model.number="params.min_piece" />
      </label>
      <label>最长片段 <b>{{ params.max_piece }}s</b>
        <input type="range" min="0" max="60" step="1" v-model.number="params.max_piece" />
      </label>
      <label>静音时长 <b>{{ params.min_silence }}s</b>
        <input type="range" min="0.2" max="2" step="0.1" v-model.number="params.min_silence" />
      </label>
      <label>合并间隔 <b>{{ params.merge_gap }}s</b>
        <input type="range" min="0.5" max="8" step="0.5" v-model.number="params.merge_gap" />
      </label>
    </div>
    <div class="muted" style="font-size:11px; margin-top:6px;">
      决定粒度的是<b>合并间隔</b>：两刀之间短于它就并成一段（不丢音频），调大→片段更长更少；
      <b>最长片段</b>把超长的整段按等分切开（填 0 = 不限制）；<b>静音阈值</b>越低越难判为静音（片段变长），
      评书带乐底时用 -40/-45 会整段切不开。
    </div>
    <div class="row" style="gap:8px; margin-top:12px;">
      <button class="btn success" :disabled="busy || (!file && !cur)" @click="start(false)">▶ 开始分片</button>
      <button v-if="cur && cur.slice_count" class="btn" :disabled="busy" @click="start(true)">↻ 按新参数重新分片</button>
    </div>
    <template v-if="job">
      <div class="progress"><div :style="{ width: (job.progress || 0) + '%' }"></div></div>
      <div class="row" style="gap:8px; align-items:center;">
        <span class="job-line" style="flex:1;">⚙️ {{ job.message }}</span>
        <button v-if="jobId" class="btn small danger" @click="stopJob">■ 停止</button>
      </div>
    </template>
  </div>

  <div v-if="!tasks.length" class="empty">还没有分片任务。上传一份长音频试试。</div>
  <div v-else class="card">
    <h3>② 任务（{{ tasks.length }}）</h3>
    <div v-for="t in tasks" :key="t.id" class="list-item" style="cursor:pointer;"
         :style="cur?.id === t.id ? 'border-left:3px solid var(--accent);' : ''" @click="openTask(t)">
      <div style="flex:1; min-width:0;">
        <b>{{ t.title }}</b>
        <span class="muted" style="font-size:11px; margin-left:8px;">{{ t.source_name }}</span>
        <div class="muted" style="font-size:11px;">
          源 {{ fmt(t.duration) }}s · 片段 {{ t.slice_count }}（标注 {{ t.labeled_count }}）· 合计 {{ fmt(t.total_sec) }}s
        </div>
      </div>
      <span class="badge" :class="t.status === 'done' ? 'ok' : t.status === 'error' ? 'err' : ''">{{ t.status }}</span>
    </div>
  </div>

  <div v-if="cur" class="card">
    <h3>③ 片段校对（{{ total }} 段，已标注 {{ labeledPct }}%）</h3>
    <div class="row" style="gap:8px; flex-wrap:wrap; margin-bottom:10px;">
      <input v-model="q" placeholder="搜索文本或文件名" style="flex:1; min-width:160px;" @keyup.enter="goto(1)" />
      <button class="btn small" @click="goto(1)">搜索</button>
      <label>视图
        <select v-model="filter" @change="goto(1)">
          <option value="all">全部片段</option>
          <option value="unlabeled">只看未标注</option>
          <option value="deleted">只看已删除（可恢复）</option>
        </select>
      </label>
      <button class="btn small" @click="retext(true)">重标空白</button>
      <button class="btn small" @click="retext(false)">全部重标</button>
      <a class="btn small primary" :href="api.slicePackUrl(cur.id)">⬇ 打包下载 zip</a>
      <a class="btn small" :href="api.sliceExportUrl(cur.id, 'list')">⬇ train.list</a>
      <a class="btn small" :href="api.sliceExportUrl(cur.id, 'jsonl')">⬇ jsonl</a>
      <button class="btn small" :class="imp.open ? '' : 'primary'" @click="toggleImport">
        🎯 导入为训练数据{{ imp.open ? ' ▾' : ' ▸' }}
      </button>
      <button class="btn small danger" @click="removeTask">删除任务</button>
    </div>
    <div v-if="cur.error" class="job-line" style="color:var(--err);">✗ {{ cur.error }}</div>
    <div v-if="cur.unlabeled_count" class="job-line" style="color:#d97706;">
      有 {{ cur.unlabeled_count }} 段没有标注文字：可点「重标空白」，或手动补写。
    </div>

    <div v-if="imp.open" class="card" style="margin:0 0 12px; background:rgba(255,255,255,.03);">
      <div class="row" style="gap:10px; flex-wrap:wrap; align-items:flex-end;">
        <label>目标数字人
          <select v-model="imp.pick" style="min-width:170px;">
            <option value="">请选择…</option>
            <option :value="NEW_HUMAN">➕ 新建一个</option>
            <option v-for="h in humans" :key="h.id" :value="h.id">{{ h.name }}</option>
          </select>
        </label>
        <input v-if="imp.pick === NEW_HUMAN" v-model="imp.name" placeholder="新数字人名字" style="min-width:150px;" />
        <label>放进哪个数据目录
          <select v-model="imp.source">
            <option value="main">主数据（音色+语感一起练）</option>
            <option value="s1">只给 s1 语感</option>
            <option value="s2">只给 s2 音色</option>
          </select>
        </label>
        <label>重复的段怎么处理
          <select v-model="imp.mode">
            <option value="append">追加：已导入过的原样保留</option>
            <option value="overwrite">覆盖：重新复制并刷新标注</option>
          </select>
        </label>
        <button class="btn small primary" :disabled="imp.busy" @click="doImport">
          {{ imp.busy ? '导入中…' : '开始导入' }}
        </button>
      </div>
      <div class="muted" style="font-size:11px; margin-top:8px;">
        把片段和标注一起复制进该数字人的训练目录，并写进训练数据记录 —— 之后在数字人页直接点训练即可。
        改过标注重导时选「覆盖」；标注为空的段不会导入（训练需要音频与文字成对）。
      </div>
      <div v-if="imp.res" class="job-line" style="margin-top:8px;">
        ✓ {{ imp.res.message }}<br />
        <span class="muted" style="font-size:11px;">
          目录：{{ imp.res.target_dir }} · train.list 共 {{ imp.res.train_list_lines }} 行 ·
          新增 {{ imp.res.copied }} / 刷新 {{ imp.res.refreshed }} / 保留 {{ imp.res.kept }}
          <template v-if="imp.res.renamed"> / 重名改名 {{ imp.res.renamed }}</template>
        </span>
      </div>
    </div>

    <table style="width:100%; font-size:12px;">
      <thead>
        <tr class="muted" style="text-align:left;">
          <th style="width:44px;">#</th><th style="width:200px;">试听</th>
          <th>标注文本</th><th style="width:78px;">起止(秒)</th><th style="width:190px;">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in slices" :key="s.id">
          <td class="mono">{{ s.idx + 1 }}</td>
          <td><audio :src="api.audioUrl(`slicing/${cur.id}/slices/${s.name}`)" controls style="width:190px; height:30px;"></audio></td>
          <td>
            <span v-if="filter === 'deleted'" class="muted">{{ (s.text || '（无标注）').slice(0, 120) }}</span>
            <template v-else>
              <textarea v-model="drafting[s.id]" :placeholder="s.text || '（无标注，可手填）'" rows="2"
                        style="width:100%; box-sizing:border-box; font-size:12px;"></textarea>
              <span v-if="s.edited" class="badge" style="font-size:10px;">已人工修改</span>
            </template>
          </td>
          <td class="mono muted">{{ fmt(s.start) }}~{{ fmt(s.end) }}</td>
          <td>
            <div class="row" style="gap:4px; flex-wrap:wrap;">
              <template v-if="filter === 'deleted'">
                <button class="btn small primary" @click="restore(s)">↩ 恢复</button>
              </template>
              <template v-else>
                <button class="btn small primary" :disabled="drafting[s.id] === undefined" @click="saveText(s)">存</button>
                <a class="btn small" :href="api.sliceDownloadUrl(s.id)">下</a>
                <input v-model="splitAt[s.id]" type="number" step="0.1" min="0" placeholder="秒" style="width:52px; font-size:11px;" />
                <button class="btn small" @click="split(s)">拆</button>
                <button class="btn small danger" @click="del(s)">删</button>
              </template>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="!slices.length" class="empty" style="padding:14px;">这一页没有片段。</div>
    <div class="row" style="gap:8px; margin-top:10px;">
      <button class="btn small" :disabled="page <= 1" @click="goto(page - 1)">上一页</button>
      <span class="muted" style="font-size:12px;">{{ page }} / {{ pages }}</span>
      <button class="btn small" :disabled="page >= pages" @click="goto(page + 1)">下一页</button>
      <span class="muted" style="font-size:11px; margin-left:auto;">「拆」在该秒处一分为二并自动重标两片</span>
    </div>
  </div>
</template>
