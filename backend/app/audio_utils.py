"""音频工具：ffmpeg 封装（自动下载静态二进制）、波形峰值、时长、混音滤镜链。"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from .config import EXPORT_BITRATE, EXPORT_FORMAT, SAMPLE_RATE

try:
    import imageio_ffmpeg

    def ffmpeg_exe() -> str:
        return imageio_ffmpeg.get_ffmpeg_exe()

    FFMPEG_AVAILABLE = True
except Exception:  # noqa: BLE001
    FFMPEG_AVAILABLE = False

    def ffmpeg_exe() -> str:
        return "ffmpeg"


# ---------------- 基础 ----------------

def _run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        timeout=timeout,
    )


def audio_duration(path: str | Path) -> float:
    """解析 Duration 获取时长（秒）。"""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return 0.0
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(p), "-f", "null", "-"],
        capture_output=True, timeout=600,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr.decode("utf-8", "ignore"))
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def read_pcm(path: str | Path, sr: int = 8000) -> np.ndarray:
    """解码为单声道 f32 PCM（低采样率，节省内存）。"""
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-f", "f32le", "-ac", "1", "-ar", str(sr), "pipe:1",
        ],
        capture_output=True, timeout=3600,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"解码失败: {proc.stderr.decode('utf-8', 'ignore')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def waveform_peaks(path: str | Path, buckets: int = 1200) -> dict:
    """波形峰值，用于前端 WaveSurfer 展示。"""
    data = read_pcm(path)
    if len(data) == 0:
        return {"peaks": [], "duration": 0.0, "sr": 8000}
    duration = len(data) / 8000.0
    # 分桶求 max/min
    if len(data) < buckets:
        buckets = max(1, len(data))
    chunk = math.ceil(len(data) / buckets)
    peaks = []
    for i in range(buckets):
        seg = data[i * chunk:(i + 1) * chunk]
        if len(seg) == 0:
            peaks.append(0.0)
            continue
        peaks.append(round(float(np.max(np.abs(seg))), 5))
    return {"peaks": peaks, "duration": round(duration, 2), "sr": 8000}


def convert_to_wav(src: str | Path, dst: str | Path, sr: int = SAMPLE_RATE) -> str:
    """统一转为 wav（16bit 单声道）。"""
    r = _run(["-i", str(src), "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    if r.returncode != 0:
        raise RuntimeError(f"转码失败: {r.stderr.decode('utf-8', 'ignore')[-500:]}")
    return str(dst)


def extract_wav(
    src: str | Path,
    dst: str | Path,
    start: float = 0.0,
    duration: float | None = None,
    sr: int = SAMPLE_RATE,
) -> str:
    """截取片段并转为 wav（16bit 单声道）。"""
    args = ["-i", str(src), "-ss", f"{max(0.0, start):.3f}"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    args += ["-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"截取转码失败: {r.stderr.decode('utf-8', 'ignore')[-500:]}")
    return str(dst)


def probe_audio(path: str | Path) -> dict:
    """音频元信息（时长/采样率/声道）。"""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, timeout=120,
    )
    err = proc.stderr.decode("utf-8", "ignore")
    info = {"duration": audio_duration(path), "sr": 0, "channels": 0}
    m = re.search(r"(\d+)\s*Hz", err)
    if m:
        info["sr"] = int(m.group(1))
    m = re.search(r"(\d+)\s*channels?", err)
    if m:
        info["channels"] = int(m.group(1))
    return info


# ---------------- 混音 ----------------

def _atempo_chain(speed: float) -> str:
    """atempo 滤镜（支持 0.25–4 倍，超出自动拆链）。"""
    speed = max(0.25, min(4.0, speed))
    filters = []
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        filters.append("atempo=0.5")
        speed /= 0.5
    filters.append(f"atempo={speed:.6f}")
    return ",".join(filters)


def build_voice_filter(params: dict, sr: int = SAMPLE_RATE, allow_fade: bool = True) -> str:
    """根据参数构建人声处理滤镜链（语速/音调/音量/3段EQ/淡入淡出）。

    音调：asetrate 改变采样率实现变调（保持时长需要 atempo 补偿）。
    allow_fade=False 时忽略淡入淡出（用于分段级处理，淡入淡出留给全局）。
    """
    speed = float(params.get("speed", 1.0))        # 语速倍率
    pitch = float(params.get("pitch", 0))          # 音调偏移（半音）
    volume = float(params.get("volume", 1.0))      # 音量倍率
    eq = params.get("eq", {"low": 0, "mid": 0, "high": 0})  # 3 段 EQ dB
    fade_in = float(params.get("fade_in", 0.0)) if allow_fade else 0.0
    fade_out = float(params.get("fade_out", 0.0)) if allow_fade else 0.0
    duration = float(params.get("duration", 0.0))  # 已知时长（淡出定位用）

    chain: list[str] = []

    # 变调（半音 → 频率倍率 2^(n/12)）
    if abs(pitch) > 0.01:
        factor = 2 ** (pitch / 12.0)
        chain.append(f"asetrate={int(sr * factor)}")
        chain.append(f"aresample={sr}")
        chain.append(f"atempo={1.0 / factor:.6f}")

    # 语速
    if abs(speed - 1.0) > 0.01:
        chain.append(_atempo_chain(speed))

    # 音量
    if abs(volume - 1.0) > 0.01:
        chain.append(f"volume={volume:.4f}")

    # 3 段 EQ
    g_low, g_mid, g_high = float(eq.get("low", 0)), float(eq.get("mid", 0)), float(eq.get("high", 0))
    if abs(g_low) > 0.01:
        chain.append(f"bass=g={g_low:.2f}:f=250")
    if abs(g_mid) > 0.01:
        chain.append(f"equalizer=f=1000:t=q:w=1.0:g={g_mid:.2f}")
    if abs(g_high) > 0.01:
        chain.append(f"treble=g={g_high:.2f}:f=6000")

    # 淡入淡出
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0 and duration > 0:
        chain.append(f"afade=t=out:st={max(0.0, duration - fade_out):.2f}:d={fade_out:.2f}")

    return ",".join(chain) if chain else "anull"


def apply_fx(
    src: str | Path,
    out_path: str | Path,
    params: dict,
    allow_fade: bool = False,
) -> str:
    """对单个音频应用参数化 FX（语速/音调/音量/EQ，可含淡入淡出）。"""
    p = dict(params)
    p["duration"] = audio_duration(src)
    vf = build_voice_filter(p, allow_fade=allow_fade)
    if vf == "anull":
        import shutil

        shutil.copy2(src, out_path)
        return str(out_path)
    r = _run(["-i", str(src), "-af", vf, "-c:a", "pcm_s16le", str(out_path)])
    if r.returncode != 0:
        raise RuntimeError(f"FX 处理失败: {r.stderr.decode('utf-8', 'ignore')[-800:]}")
    return str(out_path)


def build_ducking_expr(keyframes: list[dict], duration: float) -> str:
    """把闪避关键帧曲线转成 ffmpeg 音量表达式（逐帧线性插值）。

    keyframes: [{pct: 0~100 位置, level: 0~1 闪避深度}]，深度越大背景乐越弱。
    返回的表达式是背景乐音量倍率（1 - depth）。
    """
    if not keyframes:
        return "1"
    pts = sorted(
        (max(0.0, min(100.0, float(k.get("pct", 0)))), max(0.0, min(1.0, float(k.get("level", 0)))))
        for k in keyframes
    )
    if duration <= 0:
        return f"{1 - pts[0][1]:.4f}"
    # 去重相同时间点，保留最后一个值
    dedup: list[tuple[float, float]] = []
    for t, v in pts:
        if dedup and abs(dedup[-1][0] - t) < 1e-6:
            dedup[-1] = (t, v)
        else:
            dedup.append((t, v))
    if len(dedup) == 1:
        return f"{1 - dedup[0][1]:.4f}"

    # 从后往前构建分段线性表达式
    expr = f"{1 - dedup[-1][1]:.4f}"
    for i in range(len(dedup) - 2, -1, -1):
        t0, v0 = dedup[i]
        t1, v1 = dedup[i + 1]
        tt0 = t0 / 100 * duration
        tt1 = t1 / 100 * duration
        if tt1 - tt0 > 1e-6:
            slope = (v1 - v0) / (tt1 - tt0)
            seg = f"({1 - v0:.4f}{slope:+.5f}*(t-{tt0:.4f}))"
        else:
            seg = f"{1 - v1:.4f}"
        expr = f"if(lt(t,{tt0:.4f}),{1 - v0:.4f},{seg})"
    return expr


def mix_voice_with_bgm(
    voice_path: str | Path,
    bgm_path: str | Path | None,
    params: dict,
    out_path: str | Path,
    fmt: str = EXPORT_FORMAT,
) -> str:
    """人声 + 背景音乐混音输出。返回输出路径。

    params 额外支持: bgm_volume(倍率), ducking(0-1 人声闪避强度),
    ducking_mode(off/sidechain/curve), ducking_curve([{pct, level}])
    """
    vf = build_voice_filter(params)
    bgm_vol = float(params.get("bgm_volume", 0.25))
    ducking = float(params.get("ducking", 0.0))
    ducking_mode = params.get("ducking_mode", "sidechain" if ducking > 0.02 else "off")
    curve = params.get("ducking_curve") or []
    duration = float(params.get("duration", 0.0))

    if bgm_path and Path(bgm_path).exists():
        if ducking_mode == "curve" and curve and duration > 0:
            # 自定义闪避曲线：背景乐音量 = bgm_volume * (1 - depth(t))
            expr = build_ducking_expr(curve, duration)
            filter_complex = (
                f"[0:a]{vf}[voice];"
                f"[1:a]volume={bgm_vol:.4f}[bgm];"
                f"[bgm]volume=volume='{expr}':eval=frame[ducked];"
                f"[ducked][voice]amix=inputs=2:duration=longest:dropout_transition=3[aout]"
            )
        elif ducking_mode == "sidechain" and ducking > 0.02:
            # 人声闪避：背景乐随人声自动压低
            filter_complex = (
                f"[0:a]{vf}[voice];"
                f"[1:a]volume={bgm_vol:.4f}[bgm];"
                f"[bgm][voice]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400:"
                f"makeup={1.0 + ducking * 2.0:.2f}[ducked];"
                f"[ducked][voice]amix=inputs=2:duration=longest:dropout_transition=3[aout]"
            )
        else:
            filter_complex = (
                f"[0:a]{vf}[voice];"
                f"[1:a]volume={bgm_vol:.4f}[bgm];"
                f"[bgm][voice]amix=inputs=2:duration=longest:dropout_transition=3[aout]"
            )
        args = ["-i", str(voice_path), "-i", str(bgm_path), "-filter_complex", filter_complex]
    else:
        args = ["-i", str(voice_path), "-af", vf]

    args += ["-map", "[aout]" if (bgm_path and Path(bgm_path).exists()) else "0:a"]
    if fmt == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", EXPORT_BITRATE]
    else:
        args += ["-c:a", "pcm_s16le"]
    args.append(str(out_path))

    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"混音失败: {r.stderr.decode('utf-8', 'ignore')[-800:]}")
    return str(out_path)


def slice_audio(src: str | Path, out_path: str | Path, start: float, end: float, fmt: str = EXPORT_FORMAT) -> str:
    """截取音频片段 [start, end) 秒。"""
    args = ["-i", str(src), "-ss", f"{max(0.0, start):.3f}", "-to", f"{max(start + 0.05, end):.3f}"]
    if fmt == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", EXPORT_BITRATE]
    else:
        args += ["-c:a", "pcm_s16le"]
    args.append(str(out_path))
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(f"截取失败: {r.stderr.decode('utf-8', 'ignore')[-800:]}")
    return str(out_path)


def make_silence(out_path: str | Path, seconds: float, sr: int = SAMPLE_RATE) -> str:
    """生成静音 wav。"""
    r = _run([
        "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono",
        "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", str(out_path),
    ])
    if r.returncode != 0:
        raise RuntimeError(f"生成静音失败: {r.stderr.decode('utf-8', 'ignore')[-500:]}")
    return str(out_path)


def concat_segments(paths: list[str | Path], out_path: str | Path, fmt: str = EXPORT_FORMAT) -> str:
    """拼接多个音频文件（统一重编码）。"""
    if len(paths) == 1:
        src = str(paths[0])
        args = ["-i", src]
        if fmt == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", EXPORT_BITRATE]
        args.append(str(out_path))
        r = _run(args)
    else:
        lst = Path(out_path).parent / f"{Path(out_path).stem}_concat.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{Path(p).resolve().as_posix()}'\n")
        args = ["-f", "concat", "-safe", "0", "-i", str(lst)]
        if fmt == "mp3":
            args += ["-c:a", "libmp3lame", "-b:a", EXPORT_BITRATE]
        args.append(str(out_path))
        r = _run(args)
        lst.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"拼接失败: {r.stderr.decode('utf-8', 'ignore')[-800:]}")
    return str(out_path)


def save_peaks_cache(path: str | Path, cache_file: str | Path):
    peaks = waveform_peaks(path)
    Path(cache_file).write_text(json.dumps(peaks), encoding="utf-8")
    return peaks
