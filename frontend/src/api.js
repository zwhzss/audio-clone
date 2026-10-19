const BASE = '/api'

async function handle(r) {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const j = await r.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || j)
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  const ct = r.headers.get('content-type') || ''
  return ct.includes('application/json') ? r.json() : r
}

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  return handle(await fetch(BASE + path, { ...opts, headers }))
}

async function upload(path, formData) {
  return handle(await fetch(BASE + path, { method: 'POST', body: formData }))
}

export const api = {
  // 通用
  engines: () => req('/voices/engines/info'),
  jobs: () => req('/jobs'),
  job: (id) => req(`/jobs/${id}`),
  cancelJob: (id) => req(`/jobs/${id}`, { method: 'DELETE' }),

  // 声音库
  voices: () => req('/voices'),
  createVoice: (b) => req('/voices', { method: 'POST', body: JSON.stringify(b) }),
  updateVoice: (id, b) => req(`/voices/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  uploadRefAudio: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload(`/voices/${id}/ref-audio`, fd)
  },
  cloneVoice: (id) => req(`/voices/${id}/clone`, { method: 'POST' }),
  uploadTrainAudio: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload(`/voices/${id}/train-audio`, fd)
  },
  trainVoice: (id, b) => req(`/voices/${id}/train`, { method: 'POST', body: JSON.stringify(b) }),
  deleteVoice: (id) => req(`/voices/${id}`, { method: 'DELETE' }),

  // 文稿
  documents: () => req('/documents'),
  createDocument: (b) => req('/documents', { method: 'POST', body: JSON.stringify(b) }),
  uploadDocument: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload('/documents/upload', fd)
  },
  getDocument: (id) => req(`/documents/${id}`),
  updateDocument: (id, b) => req(`/documents/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  polishDocument: (id, style) => req(`/documents/${id}/polish`, { method: 'POST', body: JSON.stringify({ style }) }),
  deleteDocument: (id) => req(`/documents/${id}`, { method: 'DELETE' }),

  // 合成
  tts: (b) => req('/synthesis/tts', { method: 'POST', body: JSON.stringify(b) }),
  vc: (b) => req('/synthesis/vc', { method: 'POST', body: JSON.stringify(b) }),
  compare: (b) => req('/synthesis/compare', { method: 'POST', body: JSON.stringify(b) }),
  mixRender: (b) => req('/mix/render', { method: 'POST', body: JSON.stringify(b) }),
  mixDefaults: () => req('/mix/defaults'),

  // 项目
  projects: () => req('/projects'),
  createProject: (b) => req('/projects', { method: 'POST', body: JSON.stringify(b) }),
  getProject: (id) => req(`/projects/${id}`),
  updateProject: (id, b) => req(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  uploadBgm: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload(`/projects/${id}/bgm`, fd)
  },
  generateProject: (id, resume) => req(`/projects/${id}/generate${resume ? '?resume=1' : ''}`, { method: 'POST' }),
  renderProject: (id) => req(`/projects/${id}/render`, { method: 'POST' }),
  deleteProject: (id) => req(`/projects/${id}`, { method: 'DELETE' }),
  // 分段级参数
  setSegmentParams: (id, idx, params) => req(`/projects/${id}/segments/${idx}`, { method: 'PATCH', body: JSON.stringify({ params }) }),
  setSegmentText: (id, idx, text) => req(`/projects/${id}/segments/${idx}`, { method: 'PATCH', body: JSON.stringify({ text }) }),
  deleteSegment: (id, idx) => req(`/projects/${id}/segments/${idx}`, { method: 'DELETE' }),
  resynthSegment: (id, idx) => req(`/projects/${id}/segments/${idx}/resynth`, { method: 'POST' }),
  splitSegments: (id) => req(`/projects/${id}/segments/split`, { method: 'POST' }),
  clearSegmentParams: (id, idx) => req(`/projects/${id}/segments/${idx}/params`, { method: 'DELETE' }),
  // 章节
  chapters: (id) => req(`/projects/${id}/chapters`),
  saveChapters: (id, chapters) => req(`/projects/${id}/chapters`, { method: 'POST', body: JSON.stringify({ chapters }) }),
  exportChapters: (id, format, split) => req(`/projects/${id}/chapters/export`, { method: 'POST', body: JSON.stringify({ format, split }) }),

  // 设置
  getSettings: () => req('/settings'),
  saveSettings: (values) => req('/settings', { method: 'POST', body: JSON.stringify({ values }) }),
  testLlm: () => req('/settings/test-llm', { method: 'POST' }),

  // 模型库
  models: (kind = '') => req(`/models${kind ? `?kind=${kind}` : ''}`),
  modelPresets: () => req('/models/presets'),
  createModel: (b) => req('/models', { method: 'POST', body: JSON.stringify(b) }),
  updateModel: (id, b) => req(`/models/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  activateModel: (id) => req(`/models/${id}/activate`, { method: 'POST' }),
  deactivateModel: (id) => req(`/models/${id}/deactivate`, { method: 'POST' }),
  deleteModel: (id) => req(`/models/${id}`, { method: 'DELETE' }),
  testModel: (id) => req(`/models/${id}/test`, { method: 'POST' }),

  // 资源
  audioUrl: (rel) => {
    // 兼容绝对路径：提取 data/ 之后的相对路径
    if (rel && rel.length >= 2 && rel[1] === ':') {
      const idx = rel.replace(/\\/g, '/').indexOf('/data/')
      if (idx >= 0) rel = rel.slice(idx + 6) // skip '/data/'
    }
    return `${BASE}/audio/${rel}`
  },
  peaks: (rel) => req(`/audio/${rel}/peaks`),
  finalUrl: (id) => `${BASE}/projects/${id}/final`,

  // 歌曲分离
  sepEngines: () => req('/separation/engines'),
  uploadSepSong: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload('/separation/upload', fd)
  },
  runSepTask: (taskDir, b) => req(`/separation/tasks/${taskDir}/run`, { method: 'POST', body: JSON.stringify(b) }),
  sepTasks: (page = 1, size = 10, q = '') => req(`/separation/tasks?page=${page}&size=${size}&q=${encodeURIComponent(q)}`),
  deleteSepTask: (taskDir) => req(`/separation/tasks/${taskDir}`, { method: 'DELETE' }),
  toggleSepFavorite: (taskDir) => req(`/separation/tasks/${taskDir}/favorite`, { method: 'PATCH' }),

  // 音频分片（长音频 → 自动分片 + 自动标注）
  sliceUpload: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload(`/slicing/upload${file.name ? `?title=${encodeURIComponent(file.name)}` : ''}`, fd)
  },
  sliceProcess: (id, b = {}) => req(`/slicing/${id}/process`, { method: 'POST', body: JSON.stringify(b) }),
  sliceTasks: (page = 1, size = 12, q = '') => req(`/slicing?page=${page}&size=${size}&q=${encodeURIComponent(q)}`),
  sliceTask: (id) => req(`/slicing/${id}`),
  sliceList: (id, page = 1, size = 50, q = '', onlyUnlabeled = false, deletedOnly = false, onlyFlagged = false) =>
    req(`/slicing/${id}/slices?page=${page}&size=${size}&q=${encodeURIComponent(q)}&only_unlabeled=${onlyUnlabeled ? 1 : 0}&deleted_only=${deletedOnly ? 1 : 0}&only_flagged=${onlyFlagged ? 1 : 0}`),
  sliceStats: (id) => req(`/slicing/${id}/stats`),
  sliceUpdateText: (sid, text) => req(`/slicing/slices/${sid}`, { method: 'PATCH', body: JSON.stringify({ text }) }),
  sliceDelete: (sid) => req(`/slicing/slices/${sid}`, { method: 'DELETE' }),
  sliceRestore: (sid) => req(`/slicing/slices/${sid}/restore`, { method: 'POST' }),
  sliceSplit: (sid, at, retext = true) => req(`/slicing/slices/${sid}/split`, { method: 'POST', body: JSON.stringify({ at, retext }) }),
  sliceRetext: (id, onlyEmpty = true) => req(`/slicing/${id}/retext?only_empty=${onlyEmpty ? 1 : 0}`, { method: 'POST' }),
  sliceDeleteTask: (id) => req(`/slicing/${id}`, { method: 'DELETE' }),
  sliceRenameTask: (id, title) => req(`/slicing/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) }),
  sliceDownloadUrl: (sid) => `${BASE}/slicing/slices/${sid}/download`,
  slicePackUrl: (id) => `${BASE}/slicing/${id}/download`,
  sliceExportUrl: (id, fmt) => `${BASE}/slicing/${id}/export?fmt=${fmt}`,
  sliceImport: (id, b) => req(`/slicing/${id}/import`, { method: 'POST', body: JSON.stringify(b) }),

  // 音频润色
  uploadPolish: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return upload('/polish/upload', fd)
  },
  renderPolish: (taskDir, b) => req(`/polish/${taskDir}/render`, { method: 'POST', body: JSON.stringify(b) }),
  polishTasks: () => req('/polish/tasks'),

  // 数字人
  humans: () => req('/humans'),
  createHuman: (b) => req('/humans', { method: 'POST', body: JSON.stringify(b) }),
  human: (id) => req(`/humans/${id}`),
  updateHuman: (id, b) => req(`/humans/${id}`, { method: 'PATCH', body: JSON.stringify(b) }),
  deleteHuman: (id) => req(`/humans/${id}`, { method: 'DELETE' }),
  humanDataQuality: (id) => req(`/humans/${id}/data-quality`),
  humanHistory: (id) => req(`/humans/${id}/history`),
  humanCheckDuplicates: (id) => req(`/humans/${id}/check-duplicates`),
  availableModels: () => req('/humans/presets/models'),
  humanDataList: (id, page = 1, size = 20, source = 'main') => req(`/humans/${id}/data-list?page=${page}&size=${size}&source=${source}`),
  humanSaveText: (id, idx, text, audio = '', source = 'main') => req(`/humans/${id}/data-text?source=${source}`, { method: 'POST', body: JSON.stringify({ idx, text, audio }) }),
  humanSliceDelete: (id, audio, source = 'main') => req(`/humans/${id}/data-slice-delete?source=${source}`, { method: 'POST', body: JSON.stringify({ audio }) }),
  humanDataUpload: (id, file, mode = 'replace', source = 'main') => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('mode', mode)
    fd.append('source', source)
    return upload(`/humans/${id}/data-upload`, fd)
  },
  humanDataProcess: (id, mode = 'replace', source = 'main', opts = {}) => {
    const q = new URLSearchParams({ mode, source, slice_mode: opts.mode || 'silence',
                                    granularity: opts.granularity || 'para' })
    if (opts.hotwords) q.set('hotwords', opts.hotwords)
    return req(`/humans/${id}/data-process?${q.toString()}`, { method: 'POST' })
  },
  trainHuman: (id, b) => req(`/humans/${id}/train`, { method: 'POST', body: JSON.stringify(b) }),
  humanBaseModels: (id) => req(`/humans/${id}/base-models`),
  humanCheckpoints: (id) => req(`/humans/${id}/checkpoints`),
  setCheckpoint: (id, b) => req(`/humans/${id}/checkpoint`, { method: 'POST', body: JSON.stringify(b) }),
}
