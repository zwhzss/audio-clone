"""音频分片与标注：把「按静音切分 + ASR 自动标注」从数字人训练流程里独立出来。

对外提供两张表（SliceTask / AudioSlice）与两个可复用服务：
  slice_by_silence()  按静音切分长音频（可调灵敏度/最短/最长）
  asr_label_batch()   批量自动标注（优先 GPT-SoVITS 官方 funasr，带标点；退回常驻 sidecar）

单片重标（拆分后只需识别两片）走 app.asr_check 的常驻 sidecar，进程复用后每片约 0.2 秒。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

from .db import Base
from .models import _now, _uid

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg")
# 默认值取「训练素材」档：180 秒真实评书实测 40 段、中位 4.19 秒、100% 落在 3~10 秒、覆盖率 100%
DEFAULTS = {
    "noise_db": -35.0,     # 低于此音量视为静音
    "min_silence": 0.5,    # 连续静音超过这么多秒才作为候选切割点
    "min_piece": 1.0,      # 短于这么多秒的片段丢弃
    "max_piece": 10.0,     # 长于这么多秒强制细分（0 = 不限制）
    "merge_gap": 3.0,      # 两刀之间不足这么多秒就并成一段（并，不是丢）
    "lang": "zh",
}


class SliceTask(Base):
    """一个分片任务：一份源音频 + 它切出来的所有带标注片段。"""

    __tablename__ = "slice_tasks"

    id = Column(String(16), primary_key=True, default=_uid)
    title = Column(String(200), default="")
    source_name = Column(String(300), default="")
    status = Column(String(20), default="ready")   # ready/slicing/labeling/done/error
    error = Column(Text, default="")
    params = Column(JSON, default=dict)
    lang = Column(String(10), default="zh")
    duration = Column(Float, default=0.0)
    slice_count = Column(Integer, default=0)
    labeled_count = Column(Integer, default=0)
    total_sec = Column(Float, default=0.0)
    note = Column(Text, default="")                # ASR 降级等提示
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = str(self.created_at) if self.created_at else ""
        d["finished_at"] = str(self.finished_at) if self.finished_at else ""
        return d


class AudioSlice(Base):
    """一个片段：文件名 + 标注文本 + 在源音频里的起止秒。"""

    __tablename__ = "audio_slices"

    id = Column(String(16), primary_key=True, default=_uid)
    task_id = Column(String(16), index=True)
    idx = Column(Integer, default=0)
    name = Column(String(300), default="")
    text = Column(Text, default="")
    start = Column(Float, default=0.0)
    end = Column(Float, default=0.0)
    duration = Column(Float, default=0.0)
    is_deleted = Column(Integer, default=0)
    edited = Column(Integer, default=0)            # 1 = 人工改过标注
    created_at = Column(DateTime, default=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = str(self.created_at) if self.created_at else ""
        return d


@dataclass
class Piece:
    name: str
    path: Path
    start: float
    end: float
    duration: float


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def media_duration(path: str | Path) -> float:
    from .audio_utils import audio_duration

    try:
        return float(audio_duration(str(path)))
    except Exception:
        return 0.0


def _silence_ranges(exe: str, src: Path, noise_db: float, min_silence: float) -> list[tuple[float, float]]:
    r = subprocess.run(
        [exe, "-i", str(src), "-af", f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, timeout=1800, text=True, encoding="utf-8", errors="replace",
    )
    out, cur = [], None
    for line in (r.stderr or "").splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            out.append((cur, float(m.group(1))))
            cur = None
    return out


def _cuts_from_silence(silence, total: float, merge_gap: float, max_piece: float) -> list[tuple[float, float]]:
    """静音中点作为候选切点 -> 语音区间。

    merge_gap 的语义必须是**并入**：间隔不够长就不在这里下刀、继续往后累积。
    旧写法在这里无条件推进 prev，等于把这段语音直接丢掉 —— 实测 merge_gap 从 1.5 调到 3.0
    会让覆盖率从 95.6% 掉到 51.4%，"片段都落在 3~10 秒"是靠扔掉一半语音换来的假象。
    """
    spans, start = [], 0.0
    for s, e in silence:
        mid = (s + e) / 2
        if mid - start >= merge_gap:
            spans.append((start, mid))
            start = mid                       # 只有真的下刀才前进
    if spans and total - start < merge_gap:
        spans[-1] = (spans[-1][0], total)     # 结尾太短就并进最后一段，别丢
    elif total - start >= merge_gap:
        spans.append((start, total))
    if not spans:
        spans = [(0.0, total)]
    if max_piece and max_piece > 0:
        out = []
        for s, e in spans:                     # 长音频里可能整段无静音：按上限等分
            n = int((e - s) // max_piece) + (1 if (e - s) % max_piece else 0)
            if n <= 1:
                out.append((s, e))
                continue
            step = (e - s) / n
            out.extend((s + i * step, s + (i + 1) * step) for i in range(n))
        spans = out
    return spans


def slice_by_silence(src: Path, out_dir: Path, params: dict | None = None,
                     start_index: int = 1, progress=None) -> list[Piece]:
    """把一个长音频按静音切成若干 wav 片段。返回 Piece 列表（含在源文件里的起止秒）。"""
    p = {**DEFAULTS, **(params or {})}
    exe = ffmpeg_exe()
    out_dir.mkdir(parents=True, exist_ok=True)
    total = media_duration(src)
    if total <= 0:
        raise RuntimeError("读不到音频时长，文件可能已损坏或格式不支持")
    silence = _silence_ranges(exe, src, float(p["noise_db"]), float(p["min_silence"]))
    spans = _cuts_from_silence(silence, total, float(p["merge_gap"]), float(p["max_piece"]))
    pieces: list[Piece] = []
    n = start_index
    for i, (s, e) in enumerate(spans):
        if e - s < float(p["min_piece"]):
            continue
        name = f"{n:03d}_{src.stem}_seg{i + 1}.wav"
        dst = out_dir / name
        r = subprocess.run(
            [exe, "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", str(src),
             "-ar", "32000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, timeout=600,
        )
        if r.returncode != 0 or not dst.exists() or dst.stat().st_size <= 2000:
            dst.unlink(missing_ok=True)
            continue
        pieces.append(Piece(name, dst, round(s, 3), round(e, 3), round(media_duration(dst), 3)))
        n += 1
        if progress and len(spans):
            progress(5 + int((i + 1) / len(spans) * 55), f"切片 {i + 1}/{len(spans)}…")
    return pieces


def cut_piece(src: Path, dst: Path, start: float, end: float) -> bool:
    """从源音频里截取一段（拆分单片时用）。"""
    r = subprocess.run(
        [ffmpeg_exe(), "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
         "-ar", "32000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, timeout=600,
    )
    ok = r.returncode == 0 and dst.exists() and dst.stat().st_size > 2000
    if not ok:
        dst.unlink(missing_ok=True)
    return ok


def asr_label_batch(wav_paths: list[Path], work_dir: Path, lang: str = "zh") -> tuple[dict, str]:
    """批量标注。返回 ({文件名: 文本}, 说明)。

    优先用 GPT-SoVITS 自带的 funasr_asr.py：它带标点模型，标注可直接当训练文本用。
    环境/脚本缺失时退回常驻 sidecar（无标点），仍失败就返回空表让界面转人工标注。
    """
    if not wav_paths:
        return {}, ""
    from .config import STORAGE
    from .gpt_sovits_train import GPT_SOVITS_ROOT, PYTHON as GS_PYTHON

    script = Path(GPT_SOVITS_ROOT) / "tools" / "asr" / "funasr_asr.py"
    work_dir = Path(work_dir)
    shutil.rmtree(work_dir, ignore_errors=True)
    in_dir, out_dir = work_dir / "in", work_dir / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    for w in wav_paths:
        shutil.copy2(w, in_dir / w.name)

    if Path(str(GS_PYTHON)).exists() and script.exists():
        try:
            env = dict(__import__("os").environ)
            env["PYTHONUTF8"] = "1"
            libbin = Path(str(GS_PYTHON)).parent.parent / "Library" / "bin"
            if libbin.is_dir():
                env["PATH"] = str(libbin) + ";" + env.get("PATH", "")
            subprocess.run(
                [str(GS_PYTHON), str(script), "-i", str(in_dir), "-o", str(out_dir), "-l", lang],
                capture_output=True, timeout=10800, cwd=str(GPT_SOVITS_ROOT), env=env,
            )
            lists = sorted(out_dir.glob("*.list"), key=lambda p: p.stat().st_mtime, reverse=True)
            texts = {}
            if lists:
                for line in lists[0].read_text(encoding="utf-8").splitlines():
                    parts = line.split("|")
                    if len(parts) >= 4 and parts[0]:
                        # 标注文本里可能含 '|'，第 4 段之后必须全部收回
                        t = "|".join(parts[3:]).strip()
                        if t:
                            texts[Path(parts[0]).name] = t
            if texts:
                return texts, ""
            note = "（ASR 未识别到文字，请人工校对）"
        except Exception as e:  # noqa: BLE001
            note = f"（ASR 批量标注失败：{e}）"
    else:
        note = "（未找到 GPT-SoVITS 的 funasr 环境，改用本地 sidecar 逐片识别）"

    from . import asr_check

    if not asr_check.available():
        return {}, note or "（无可用的 ASR，转人工标注）"
    texts = {}
    for w in wav_paths:
        t = asr_check.transcribe(str(w)) or ""
        if t.strip():
            texts[w.name] = t.strip()
    return texts, (note if texts else "（ASR sidecar 也不可用，转人工标注）")


def asr_one(wav_path: Path) -> str:
    """单片重标（拆分出两片时用），走常驻 sidecar，进程热的时候不到 1 秒。"""
    from . import asr_check

    if asr_check.available():
        return (asr_check.transcribe(str(wav_path)) or "").strip()
    texts, _ = asr_label_batch([Path(wav_path)], Path(wav_path).parent / "_asr_one")
    return texts.get(Path(wav_path).name, "").strip()


def build_export(slices, out: Path, fmt: str = "list", lang: str = "zh") -> Path:
    """导出标注：list=GPT-SoVITS train.list（audio|speaker0|lang|text），jsonl=逐行 JSON。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "list":
        lines = [f"{s.name}|speaker0|{lang}|{(s.text or '').strip()}" for s in slices]
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return out
    payload = [{"file": s.name, "text": (s.text or "").strip(), "start": s.start,
                "end": s.end, "duration": s.duration} for s in slices]
    out.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in payload), encoding="utf-8")
    return out
