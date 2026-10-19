/**
 * 实时预览引擎：浏览器内对声音轨 + 背景乐做参数化混音（语速/音调/音量/EQ/背景乐音量），
 * 与服务端渲染参数一一对应（SoundTouch 变调变速 + Biquad EQ 链）。
 */
import { PitchShifter } from 'soundtouchjs'

export class PreviewEngine {
  constructor() {
    this.ctx = null
    this.shifter = null
    this.bgmSource = null
    this.bgmGain = null
    this.voiceGain = null
    this.filters = []
    this.playing = false
    this.onProgress = null
    this.onEnd = null
    this._startedAt = 0
    this._bgmOffset = 0
  }

  async load(voiceUrl, bgmUrl) {
    this.stop()
    this.ctx = new (window.AudioContext || window.webkitAudioContext)()
    const [vbuf, bgmBuf] = await Promise.all([
      this._fetchBuffer(voiceUrl),
      bgmUrl ? this._fetchBuffer(bgmUrl) : Promise.resolve(null),
    ])
    this.voiceBuffer = vbuf
    this.bgmBuffer = bgmBuf
  }

  async _fetchBuffer(url) {
    const resp = await fetch(url)
    const ab = await resp.arrayBuffer()
    return await this.ctx.decodeAudioData(ab)
  }

  /** 构建节点链（每次播放前调用） */
  _buildChain(params) {
    const ctx = this.ctx
    // 人声：PitchShifter → 音量 → EQ(低/中/高) → 输出
    this.shifter = new PitchShifter(ctx, this.voiceBuffer, 1024, () => {
      this.playing = false
      if (this.onEnd) this.onEnd()
    })
    this.voiceGain = ctx.createGain()
    this.filters = []
    const low = ctx.createBiquadFilter()
    low.type = 'lowshelf'; low.frequency.value = 250
    const mid = ctx.createBiquadFilter()
    mid.type = 'peaking'; mid.frequency.value = 1000; mid.Q.value = 1.0
    const high = ctx.createBiquadFilter()
    high.type = 'highshelf'; high.frequency.value = 6000
    this.filters = [low, mid, high]
    this.shifter.connect(this.voiceGain)
    let node = this.voiceGain
    for (const f of this.filters) { node.connect(f); node = f }
    node.connect(ctx.destination)

    this.shifter.on('play', (d) => {
      if (this.onProgress) this.onProgress(d.timePlayed, d.percentagePlayed)
    })

    // 背景乐：bufferSource → 音量 → 输出
    if (this.bgmBuffer) {
      this.bgmSource = ctx.createBufferSource()
      this.bgmSource.buffer = this.bgmBuffer
      this.bgmSource.loop = true
      this.bgmGain = ctx.createGain()
      this.bgmSource.connect(this.bgmGain)
      this.bgmGain.connect(ctx.destination)
    }

    this._applyParams(params)
  }

  _applyParams(params) {
    if (!this.shifter) return
    this.shifter.tempo = clamp(params.speed, 0.25, 4)
    this.shifter.pitchSemitones = clamp(params.pitch, -12, 12)
    this.voiceGain.gain.value = clamp(params.volume, 0, 3)
    const eq = params.eq || {}
    this.filters[0].gain.value = clamp(eq.low ?? 0, -12, 12)
    this.filters[1].gain.value = clamp(eq.mid ?? 0, -12, 12)
    this.filters[2].gain.value = clamp(eq.high ?? 0, -12, 12)
    if (this.bgmGain) this.bgmGain.gain.value = clamp(params.bgm_volume ?? 0.25, 0, 1)
  }

  play(params, bgmFromStart = true) {
    if (!this.ctx || !this.voiceBuffer) return
    if (this.ctx.state === 'suspended') this.ctx.resume()
    this.stop()
    this._buildChain(params)
    const now = this.ctx.currentTime
    if (this.bgmSource) {
      this.bgmSource.start(now)
      if (this.bgmBuffer && this.bgmGain) {
        this.bgmGain.gain.setValueAtTime(0, now)
        const g = clamp(params.bgm_volume ?? 0.25, 0, 1)
        this.bgmGain.gain.linearRampToValueAtTime(g, now + 0.8) // 背景乐淡入
      }
    }
    this.playing = true
  }

  setParams(params) {
    this._applyParams(params)
  }

  stop() {
    if (this.shifter) {
      try { this.shifter.off('play') } catch { /* ignore */ }
      try { this.shifter.disconnect() } catch { /* ignore */ }
      this.shifter = null
    }
    if (this.bgmSource) {
      try { this.bgmSource.stop() } catch { /* ignore */ }
      this.bgmSource = null
    }
    this.playing = false
  }

  dispose() {
    this.stop()
    if (this.ctx) { this.ctx.close().catch(() => {}) ; this.ctx = null }
  }
}

function clamp(v, min, max) {
  return Math.min(max, Math.max(min, Number(v) || 0))
}
