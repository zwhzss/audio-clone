<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'
import { refreshAll } from '../store.js'

const kind = ref('voice')
const models = ref([])
const presets = ref([])
const testing = ref('')
const showForm = ref(false)
const editingId = ref(null)
const estimateChars = ref(10) // 预计字数（万字）

const form = reactive({
  name: '', provider: 'custom', model: '', api_key: '', base_url: '',
  price: 0, price_output: 0, price_unit: '万字符', desc: '', extra: '', activate: true,
})

const kindLabel = { voice: '语音合成/克隆', llm: '文稿润色（LLM）' }

const voiceModels = computed(() => models.value.filter((m) => m.kind === 'voice'))
const llmModels = computed(() => models.value.filter((m) => m.kind === 'llm'))
const activeModel = computed(() => models.value.find((m) => m.kind === kind.value && m.is_active))

const providerName = (p) =>
  ({ edge: 'Edge', openvino: 'OpenVINO', minimax: 'MiniMax', fish: 'Fish Audio', deepseek: 'DeepSeek', qwen: '通义千问', glm: '智谱', kimi: 'Moonshot', doubao: '火山方舟', openai: 'OpenAI', volcengine: '火山引擎', azure: 'Azure', custom: '自定义' }[p] || p)

// 本地引擎不需要 key；云端/LLM 需要
const hasKey = (m) => !!m.api_key || m.provider === 'edge' || m.provider === 'openvino'

async function load() {
  models.value = await api.models()
}
onMounted(load)

// ---------- 性价比估算 ----------
const estimateRows = computed(() => {
  const rows = []
  const n = Number(estimateChars.value) || 0
  for (const m of models.value) {
    if (m.kind !== kind.value) continue
    if (m.price_unit === '免费') {
      rows.push({ ...m, est: 0, note: '免费' })
    } else if (m.kind === 'voice') {
      rows.push({ ...m, est: +(n * m.price).toFixed(2), note: `${m.price} 元/万字符` })
    } else {
      // LLM：输入按 1.2 倍字数 token，输出按 0.6 倍
      const inCost = n * 1.2 * m.price
      const outCost = n * 0.6 * (m.price_output || 0)
      rows.push({ ...m, est: +(inCost + outCost).toFixed(2), note: `¥${m.price}/${m.price_output}·百万token` })
    }
  }
  rows.sort((a, b) => a.est - b.est)
  return rows
})
const totalEst = computed(() => estimateRows.value.reduce((s, r) => s + r.est, 0))

// ---------- 表单 ----------
function openAdd() {
  editingId.value = null
  Object.assign(form, {
    name: '', provider: 'custom', model: '', api_key: '', base_url: '',
    price: 0, price_output: 0, price_unit: kind.value === 'llm' ? '百万token' : '万字符',
    desc: '', extra: '', activate: true,
  })
  showForm.value = true
}

function openEdit(m) {
  editingId.value = m.id
  Object.assign(form, {
    name: m.name, provider: m.provider, model: m.model, api_key: m.api_key,
    base_url: m.base_url, price: m.price, price_output: m.price_output,
    price_unit: m.price_unit, desc: m.desc,
    extra: m.extra && Object.keys(m.extra).length ? JSON.stringify(m.extra, null, 0) : '',
    activate: false,
  })
  showForm.value = true
}

function applyPreset(e) {
  const p = presets.value.find((x) => x.name === e.target.value)
  if (!p) return
  Object.assign(form, {
    name: p.name, provider: p.provider, model: p.model, base_url: p.base_url,
    price: p.price, price_output: p.price_output, price_unit: p.price_unit,
    desc: p.desc, extra: Object.keys(p.extra || {}).length ? JSON.stringify(p.extra) : '',
  })
}

async function submitForm() {
  try {
    let extra = {}
    if (form.extra.trim()) {
      try { extra = JSON.parse(form.extra) } catch { toast('扩展配置不是合法 JSON', 'error'); return }
    }
    const body = {
      kind: kind.value, name: form.name, provider: form.provider, model: form.model,
      api_key: form.api_key, base_url: form.base_url, price: Number(form.price) || 0,
      price_output: Number(form.price_output) || 0, price_unit: form.price_unit,
      desc: form.desc, extra, activate: form.activate,
    }
    if (editingId.value) {
      delete body.activate
      await api.updateModel(editingId.value, body)
      toast('模型已更新')
    } else {
      await api.createModel(body)
      toast('模型已添加' + (form.activate ? '并激活' : ''))
    }
    showForm.value = false
    await load()
    await refreshAll()
  } catch (e) { errToast(e) }
}

async function activate(m) {
  try {
    await api.activateModel(m.id)
    await load()
    await refreshAll()
    if (!m.api_key && m.provider !== 'edge' && m.provider !== 'openvino') {
      toast(`已激活「${m.name}」—— 但它还没有 API Key，请先编辑补充，否则实际调用会失败`, 'error', 8000)
    } else {
      toast(`已激活「${m.name}」，后续合成/润色将使用它`)
    }
  } catch (e) { errToast(e) }
}

async function deactivate(m) {
  try {
    await api.deactivateModel(m.id)
    await load()
    await refreshAll()
    toast(`已停用「${m.name}」`)
  } catch (e) { errToast(e) }
}

async function remove(m) {
  const hint = m.is_preset ? '（缺省模型，删除后可在预设中重新添加）' : ''
  if (!confirm(`确定删除模型「${m.name}」${hint}？`)) return
  try {
    await api.deleteModel(m.id)
    await load()
    await refreshAll()
    toast('已删除')
  } catch (e) { errToast(e) }
}

async function test(m) {
  testing.value = m.id
  try {
    const r = await api.testModel(m.id)
    toast(`${m.name}：${r.message}`, r.ok ? 'success' : 'error', 6000)
  } catch (e) { errToast(e) } finally { testing.value = '' }
}

function priceText(m) {
  if (m.price_unit === '免费' || (m.price === 0 && m.price_output === 0)) return '免费'
  if (m.kind === 'llm') return `¥${m.price} / ¥${m.price_output}`
  return `¥${m.price} / ${m.price_unit}`
}

watch(kind, () => { if (!showForm.value) load() })
</script>

<template>
  <div>
    <div class="page-title">🧩 商用模型配置中心</div>
    <div class="page-sub">
      管理所有可用的模型（语音合成 + 文稿润色）。同一类别同时只有一个模型处于「激活」状态，合成与润色自动使用激活模型；
      随时来回切换，结合下方费用估算对比哪个性价比高用哪个。密钥仅存本机。
    </div>

    <div class="tabs">
      <div class="tab" :class="{ active: kind === 'voice' }" @click="kind = 'voice'">🎙️ 语音模型</div>
      <div class="tab" :class="{ active: kind === 'llm' }" @click="kind = 'llm'">🧠 润色模型（LLM）</div>
    </div>

    <!-- 当前激活 -->
    <div class="card">
      <h3>⚡ 当前激活的{{ kindLabel[kind] }}</h3>
      <div v-if="activeModel" class="row" style="gap:10px;">
        <span class="badge ok" style="font-size:13px; padding:6px 14px;">{{ activeModel.name }}</span>
        <span class="badge accent">{{ providerName(activeModel.provider) }}</span>
        <span class="badge">{{ activeModel.model }}</span>
        <span class="badge" :class="activeModel.price === 0 ? 'ok' : 'warn'">{{ priceText(activeModel) }}</span>
        <span v-if="!hasKey(activeModel)" class="badge err">⚠ 缺 API Key（当前不可用）</span>
        <span class="muted" style="flex:1;">{{ activeModel.desc }}</span>
      </div>
      <div v-else class="empty" style="padding:14px;">
        {{ kind === 'llm' ? '未激活任何润色模型 —— 添加并激活一个 LLM 模型即可使用「文稿润色」' : '未激活任何语音模型（合成将回退到 Edge 免费音色）' }}
      </div>
      <div v-if="activeModel && !hasKey(activeModel)" class="hint" style="margin-top:8px; color:var(--warn);">
        ⚠ 「激活」只是选中该模型；它还没有 API Key，实际调用（合成/润色）会失败。请点「编辑」填入密钥，或点「停用」。
      </div>
    </div>

    <!-- 模型列表 -->
    <div class="card">
      <div class="row" style="justify-content:space-between; margin-bottom:10px;">
        <h3 style="margin:0;">模型列表（{{ (kind === 'voice' ? voiceModels : llmModels).length }} 个）</h3>
        <button class="btn primary" @click="openAdd">＋ 添加模型</button>
      </div>
      <table class="tbl">
        <thead>
          <tr>
            <th>名称</th><th>平台</th><th>模型</th><th>价格</th><th>状态</th><th style="width:220px;">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in (kind === 'voice' ? voiceModels : llmModels)" :key="m.id">
            <td>
              <b>{{ m.name }}</b>
              <span v-if="m.is_preset" class="badge" style="margin-left:6px;">缺省</span>
              <div class="muted" style="font-size:11px; margin-top:2px;">{{ m.desc }}</div>
            </td>
            <td>{{ providerName(m.provider) }}</td>
            <td class="mono">{{ m.model || '—' }}</td>
            <td>{{ priceText(m) }}</td>
            <td>
              <div class="row" style="gap:6px;">
                <span v-if="m.is_active" class="badge ok">✓ 激活中</span>
                <span v-if="!hasKey(m) && (m.is_active || m.provider !== 'edge' && m.provider !== 'openvino')" class="badge err">⚠ 缺密钥</span>
                <span v-if="!m.is_active && hasKey(m)" class="badge">未激活</span>
              </div>
            </td>
            <td>
              <div class="row" style="gap:6px;">
                <button v-if="!m.is_active" class="btn small primary" @click="activate(m)">激活</button>
                <button v-if="m.is_active" class="btn small" @click="deactivate(m)">停用</button>
                <button class="btn small" @click="test(m)" :disabled="testing === m.id">{{ testing === m.id ? '测试中…' : '测试' }}</button>
                <button class="btn small" @click="openEdit(m)">编辑</button>
                <button class="btn small danger" @click="remove(m)">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 性价比估算 -->
    <div class="card">
      <h3>📊 性价比估算 <span class="muted" style="font-size:12px; font-weight:400;">（按当前列表价格估算，可编辑单价后自动更新）</span></h3>
      <div class="row" style="margin-bottom:10px;">
        <span class="muted">预计{{ kind === 'voice' ? '合成' : '润色' }}量：</span>
        <input v-model.number="estimateChars" type="number" min="1" style="width:100px;" />
        <span class="muted">万字 → 各模型估算费用（升序 = 越靠前越便宜）：</span>
      </div>
      <table class="tbl">
        <thead>
          <tr><th>排名</th><th>模型</th><th>单价</th><th style="text-align:right;">估算费用</th><th>推荐</th></tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in estimateRows" :key="r.id" :style="r.is_active ? 'background:rgba(62,207,158,.06);' : ''">
            <td>{{ i + 1 }}</td>
            <td>
              {{ r.name }}
              <span v-if="r.is_active" class="badge ok" style="margin-left:6px;">激活中</span>
            </td>
            <td class="muted">{{ r.note }}</td>
            <td style="text-align:right;">
              <b :style="{ color: r.est === 0 ? 'var(--ok)' : r.est === Math.min(...estimateRows.filter(x => x.est > 0).map(x => x.est), Infinity) ? 'var(--warn)' : '' }">
                {{ r.est === 0 ? '¥0（免费）' : '¥' + r.est.toFixed(2) }}
              </b>
            </td>
            <td>
              <span v-if="r.est === 0" class="badge ok">免费最优</span>
              <span v-else-if="i === 0" class="badge warn">最便宜</span>
              <span v-else-if="r.is_active" class="badge accent">当前使用</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="hint" style="margin-top:8px;">
        💡 LLM 估算按「输入 = 字数×1.2 token、输出 = 字数×0.6 token」粗略折算；
        语音按 ¥/万字符 直乘。价格可在编辑中按最新官网价调整，估算随之更新。
      </div>
    </div>

    <!-- 添加/编辑弹窗 -->
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div class="modal" style="width:min(680px, 94vw);">
        <h3>{{ editingId ? '编辑模型' : '添加' + kindLabel[kind] }}</h3>

        <div v-if="!editingId" class="row" style="margin-bottom:12px;">
          <label class="field grow" style="margin:0;"><span>从预设模板选择（一键填充）</span>
            <select @change="applyPreset">
              <option value="">— 手动填写 —</option>
              <option v-for="p in presets.filter((x) => x.kind === kind)" :key="p.name + p.model" :value="p.name">
                {{ p.name }}（{{ p.price_unit === '免费' ? '免费' : '¥' + p.price + '/' + p.price_unit }}）{{ p.desc }}
              </option>
            </select>
          </label>
        </div>

        <div class="row">
          <label class="field grow"><span>名称 *</span>
            <input v-model="form.name" type="text" placeholder="例：MiniMax speech-02-hd" />
          </label>
          <label class="field"><span>平台</span>
            <input v-model="form.provider" type="text" placeholder="minimax / deepseek / custom" style="width:140px;" />
          </label>
        </div>
        <div class="row">
          <label class="field grow"><span>模型名</span>
            <input v-model="form.model" type="text" placeholder="speech-02-hd / deepseek-chat" />
          </label>
          <label class="field"><span>单价（元）</span>
            <input v-model.number="form.price" type="number" step="0.1" style="width:110px;" />
          </label>
          <label class="field"><span>输出单价（LLM）</span>
            <input v-model.number="form.price_output" type="number" step="0.1" style="width:110px;" />
          </label>
          <label class="field"><span>计费单位</span>
            <select v-model="form.price_unit" style="width:120px;">
              <option value="万字符">万字符</option>
              <option value="百万token">百万token</option>
              <option value="免费">免费</option>
            </select>
          </label>
        </div>
        <label class="field"><span>API Key（仅本地保存；留空表示不填）</span>
          <input v-model="form.api_key" type="password" :placeholder="editingId ? '打码值不修改，留空保留' : 'sk-...'" />
        </label>
        <label class="field"><span>Base URL（OpenAI 兼容地址；本地引擎可留空）</span>
          <input v-model="form.base_url" type="text" placeholder="https://api.deepseek.com/v1" />
        </label>
        <label class="field"><span>扩展配置（JSON，可选：minimax 填 {"group_id":"..."}；openvino 填 {"model_dir":"E:/models/...","device":"NPU"}）</span>
          <textarea v-model="form.extra" rows="2" placeholder='{"group_id":""}'></textarea>
        </label>
        <label class="field"><span>描述</span>
          <input v-model="form.desc" type="text" placeholder="一句话说明音质/特点/适用场景" />
        </label>
        <label v-if="!editingId" class="row" style="gap:8px; margin-bottom:12px; cursor:pointer;">
          <input type="checkbox" v-model="form.activate" style="accent-color:var(--accent);" />
          <span>添加后立即激活（会替换当前激活模型）</span>
        </label>

        <div class="row" style="justify-content:flex-end;">
          <button class="btn" @click="showForm = false">取消</button>
          <button class="btn primary" @click="submitForm">{{ editingId ? '保存' : '添加' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
