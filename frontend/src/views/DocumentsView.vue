<script setup>
import { reactive, ref } from 'vue'
import { store, refreshAll, pollJob } from '../store.js'
import { api } from '../api.js'
import { toast, errToast } from '../toast.js'

const showEdit = ref(false)
const editingId = ref(null)
const form = reactive({ title: '', content: '' })
const polishStyle = ref('朗诵')
const polishBusyId = ref('') // 正在润色的文稿 id
const applying = ref('')

const styles = [
  { id: '朗诵', desc: '语句通顺、节奏分明，适合朗读' },
  { id: '有声书', desc: '自然口语化，适合演播' },
  { id: '视频配音', desc: '短句明快，适合配音' },
  { id: '不改写', desc: '仅纠错别字' },
]

function openCreate() {
  editingId.value = null
  form.title = ''
  form.content = ''
  showEdit.value = true
}

function openEdit(d) {
  editingId.value = d.id
  form.title = d.title
  form.content = d.content || ''
  showEdit.value = true
}

async function submitDoc() {
  try {
    if (editingId.value) {
      await api.updateDocument(editingId.value, { title: form.title, content: form.content })
    } else {
      await api.createDocument({ title: form.title, content: form.content })
    }
    await refreshAll()
    showEdit.value = false
    toast('文稿已保存')
  } catch (e) {
    errToast(e)
  }
}

async function onUpload(e) {
  const f = e.target.files?.[0]
  if (!f) return
  try {
    await api.uploadDocument(f)
    await refreshAll()
    toast(`已导入「${f.name}」`)
  } catch (err) {
    errToast(err)
  }
}

async function polish(doc) {
  polishBusyId.value = doc.id
  try {
    const r = await api.polishDocument(doc.id, polishStyle.value)
    pollJob(r.job_id, async (j) => {
      if (j.status === 'success') {
        toast(`「${doc.title}」润色完成，LLM 花费 ¥${(j.result?.cost || 0).toFixed(5)}`)
        polishBusyId.value = ''
        await refreshAll()
      } else if (j.status === 'error') {
        polishBusyId.value = ''
        errToast(`润色失败：${j.message}`)
      }
    })
  } catch (e) {
    polishBusyId.value = ''
    errToast(e)
  }
}

async function removeDoc(d) {
  if (!confirm(`确定删除文稿「${d.title}」？`)) return
  try {
    await api.deleteDocument(d.id)
    await refreshAll()
    toast('已删除')
  } catch (e) {
    errToast(e)
  }
}

function usePolished(doc) {
  editingId.value = doc.id
  form.title = doc.title
  form.content = doc.polished || doc.content
  showEdit.value = true
  applying.value = doc.id
  setTimeout(() => (applying.value = ''), 1500)
  toast('已载入润色结果')
}
</script>

<template>
  <div>
    <div class="page-title">📄 文稿</div>
    <div class="page-sub">导入文学作品/视频脚本，调用大模型 API 润色为适合朗读的文本（默认 DeepSeek，性价比最高，费用自动统计）。</div>

    <div class="row" style="justify-content:space-between; margin-bottom:12px;">
      <h3 style="margin:0;">我的文稿</h3>
      <div class="row">
        <label class="btn" style="cursor:pointer;">
          📥 导入 txt/md/srt
          <input type="file" accept=".txt,.md,.srt" style="display:none" @change="onUpload" />
        </label>
        <button class="btn primary" @click="openCreate">＋ 新建文稿</button>
      </div>
    </div>

    <div v-if="!store.documents.length" class="empty">还没有文稿。新建或导入一份文稿开始。</div>

    <div v-for="d in store.documents" :key="d.id" class="list-item" style="align-items:flex-start;">
      <div style="flex:1; min-width:0;">
        <div class="row" style="gap:8px;">
          <span class="name">{{ d.title }}</span>
          <span v-if="d.polished" class="badge ok">已润色（{{ d.polish_style }}）</span>
          <span v-if="d.llm_cost > 0" class="badge accent">润色累计 ¥{{ d.llm_cost.toFixed(5) }}</span>
          <span v-if="polishBusyId === d.id" class="badge warn">润色中…</span>
        </div>
        <div class="sub" style="margin-top:6px;">
          {{ (d.content || '').slice(0, 100) }}…
        </div>
        <div v-if="d.polished" class="sub" style="margin-top:4px; color:var(--ok);">
          ✨ 润色后：{{ d.polished.slice(0, 100) }}…
        </div>
      </div>
      <div class="actions" style="flex-direction:column; align-items:flex-end; gap:6px;">
        <div class="row" style="gap:6px;">
          <button class="btn small" @click="openEdit(d)">编辑</button>
          <button v-if="d.polished" class="btn small" @click="usePolished(d)">载入润色稿</button>
          <button class="btn small danger" @click="removeDoc(d)">删除</button>
        </div>
        <div class="row" style="gap:6px;">
          <select v-model="polishStyle" style="width:110px; padding:4px 8px; font-size:12px;">
            <option v-for="s in styles" :key="s.id" :value="s.id">{{ s.id }}</option>
          </select>
          <button class="btn small primary" :disabled="polishBusyId === d.id" @click="polish(d)">✨ LLM 润色</button>
        </div>
      </div>
    </div>

    <!-- 编辑器 -->
    <div v-if="showEdit" class="modal-mask" @click.self="showEdit = false">
      <div class="modal" style="width:min(860px, 94vw);">
        <h3>{{ editingId ? '编辑文稿' : '新建文稿' }}</h3>
        <label class="field"><span>标题</span>
          <input v-model="form.title" type="text" />
        </label>
        <label class="field"><span>正文（可粘贴文学作品/脚本全文）</span>
          <textarea v-model="form.content" rows="14"></textarea>
        </label>
        <div class="row" style="justify-content:flex-end;">
          <button class="btn" @click="showEdit = false">关闭</button>
          <button class="btn primary" @click="submitDoc">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
