<script setup>
import { ref, computed } from 'vue'
import { store, refreshAll } from '../store.js'
import { toast } from '../toast.js'

const saved = ref(false)

const engineRows = computed(() => [
  { id: 'edge', name: 'Edge 免费音色', hint: '免费 · 联网即可' },
  { id: 'openvino', name: 'OpenVINO（NPU 端侧）', hint: '免费 · 本地推理' },
  { id: 'gpt_sovits', name: 'GPT-SoVITS（本地 GPU）', hint: '免费 · 克隆/换声' },
  { id: 'minimax', name: 'MiniMax（云）', hint: '约 ¥3~4/万字' },
  { id: 'fish', name: 'Fish Audio（云）', hint: '积分制' },
].map((e) => {
  const info = store.engines.find((x) => x.id === e.id)
  return { ...e, ok: !!info?.ok, msg: info?.check_message || '未激活/未配置' }
}))

function gotoModels() {
  localStorage.setItem('ac_view', 'models')
  location.reload()
}

async function refresh() {
  await refreshAll()
  saved.value = true
  setTimeout(() => (saved.value = false), 1500)
  toast('引擎状态已刷新')
}
</script>

<template>
  <div>
    <div class="page-title">⚙️ 设置</div>
    <div class="page-sub">所有模型配置（API 密钥、价格、激活切换）已集中到「模型库」，本页提供引擎状态与费用参考。</div>

    <div class="card">
      <h3>🧩 模型配置</h3>
      <div class="hint">
        MiniMax / Fish Audio / DeepSeek / Qwen / GLM / OpenVINO 等所有商用与本地模型，均在「模型库」中统一管理：
        添加模型 → 填写密钥 → 激活切换。合成工作台与文稿润色会自动使用当前激活的模型。
        旧版设置页中的密钥已自动迁移为缺省模型条目，无需重新配置。
      </div>
      <div class="row">
        <button class="btn primary" @click="gotoModels">→ 前往模型库配置模型</button>
        <button class="btn" @click="refresh">↻ 刷新引擎状态</button>
      </div>
    </div>

    <div class="card">
      <h3>🔌 引擎状态</h3>
      <table class="tbl">
        <thead>
          <tr><th>引擎</th><th>状态</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in engineRows" :key="e.id">
            <td><b>{{ e.name }}</b><div class="muted" style="font-size:11px;">{{ e.hint }}</div></td>
            <td>
              <span class="badge" :class="e.ok ? 'ok' : 'err'">{{ e.ok ? '可用' : '未激活/未配置' }}</span>
            </td>
            <td class="muted">{{ e.msg }}</td>
          </tr>
        </tbody>
      </table>
      <div class="hint" style="margin-top:8px;">状态随「模型库」中的激活模型实时变化。</div>
    </div>

    <div class="card">
      <h3>🧾 费用参考（2025 年市场参考价，可在模型库中按最新价调整）</h3>
      <table class="tbl">
        <thead>
          <tr><th>方案</th><th>用途</th><th>费用</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr><td><b>Edge 免费音色</b></td><td>初版音频/日常合成</td><td>免费</td><td>微软神经语音，联网即可</td></tr>
          <tr><td><b>OpenVINO（NPU）</b></td><td>端侧合成</td><td>免费</td><td>本地 NPU/CPU 推理，免联网，需自备模型</td></tr>
          <tr><td><b>GPT-SoVITS</b></td><td>声音克隆 + 音色转换</td><td>免费（本地 GPU）</td><td>10s 参考音频零样本克隆；8GB 显存可跑</td></tr>
          <tr><td><b>DeepSeek V4-Flash</b></td><td>文稿润色</td><td>约 ¥4/16 百万token</td><td>官方最新模型（deepseek-chat 已更名），峰谷定价</td></tr>
          <tr><td><b>Qwen-Turbo / GLM-4-Flash</b></td><td>文稿润色</td><td>¥0.3 / 免费</td><td>更便宜的选择</td></tr>
          <tr><td><b>MiniMax</b></td><td>云语音克隆</td><td>约 ¥3~4 / 万字</td><td>请求时复刻，中文音质好</td></tr>
          <tr><td><b>Fish Audio</b></td><td>云语音克隆</td><td>积分制，约 ¥5 / 万字</td><td>支持平台训练专属音色</td></tr>
        </tbody>
      </table>
      <div class="hint" style="margin-top:8px;">💡 推荐组合：本地 GPT-SoVITS 克隆（免费）+ Edge 初版 + GLM-4-Flash/DeepSeek 润色 —— 几乎零成本；模型库中可随时切换对比。</div>
    </div>
  </div>
</template>
