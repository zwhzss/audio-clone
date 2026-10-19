"""合成路由：单段 TTS、音色转换、音频文件服务（支持 Range 播放）。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import asr_check
from ..audio_utils import audio_duration, waveform_peaks
from ..config import STORAGE
from ..db import get_db
from ..engines import get_engine
from ..jobs import enqueue
from ..models import Voice

router = APIRouter(prefix="/api", tags=["synthesis"])

# 允许通过 /api/audio 访问的存储目录
_SERVE_DIRS = {
    "voices": STORAGE["voices"],
    "drafts": STORAGE["drafts"],
    "projects": STORAGE["projects"],
    "uploads": STORAGE["uploads"],
    "separation": STORAGE["separation"],
    "slicing": STORAGE["slicing"],
    "polish": STORAGE["polish"],
    "cache": STORAGE["cache"],
}

_MEDIA = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
    ".opus": "audio/opus", ".zip": "application/zip", ".cue": "text/plain",
    ".lrc": "text/plain", ".json": "application/json",
}


class TTSBody(BaseModel):
    voice_id: str
    text: str
    title: str = "合成"


class VCBody(BaseModel):
    voice_id: str
    audio_path: str   # 相对路径，如 drafts/xxx.wav
    title: str = "音色转换"


class CompareBody(BaseModel):
    voice_ids: list[str]
    text: str
    title: str = "对比试听"


@router.post("/synthesis/compare")
def compare_tts(body: CompareBody, db: Session = Depends(get_db)):
    """多人音色对比：同一文本用多个声音分别合成（异步），便于 AB 对比。"""
    if not body.voice_ids:
        raise HTTPException(400, "请至少选择一个声音")
    if len(body.voice_ids) > 6:
        raise HTTPException(400, "一次最多对比 6 个声音")
    if not body.text.strip():
        raise HTTPException(400, "文本为空")
    voices = []
    for vid in body.voice_ids:
        v = db.get(Voice, vid)
        if not v:
            raise HTTPException(404, f"声音不存在: {vid}")
        voices.append(v)

    def _run(update):
        items = []
        total = len(voices)
        for i, v in enumerate(voices):
            update(int(i / total * 95), f"合成 {v.name}（{i + 1}/{total}）…")
            engine = get_engine(v.source)
            out_dir = STORAGE["drafts"] / f"cmp_{uuid.uuid4().hex[:8]}"
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                r = engine.tts(v, body.text, str(out_dir / "out.mp3"), None)
                rel = Path(r["path"]).relative_to(STORAGE["drafts"].parent).as_posix()
                items.append({
                    "voice_id": v.id,
                    "voice_name": v.name,
                    "voice_source": v.source,
                    "path": rel,
                    "cost": r.get("cost", 0.0),
                    "duration": r.get("duration", 0.0),
                })
            except Exception as e:  # noqa: BLE001
                items.append({"voice_id": v.id, "voice_name": v.name, "voice_source": v.source,
                              "path": "", "cost": 0.0, "error": str(e)})
        update(100, f"对比完成（{len(items)} 个声音）")
        return {"items": items}

    job_id = enqueue("compare", f"{body.title}（{len(voices)} 个声音）", _run,
                     gpu=any(v.source in ("gpt_sovits", "openvino") for v in voices))
    return {"job_id": job_id}


@router.post("/synthesis/tts")
def tts(body: TTSBody, db: Session = Depends(get_db)):
    """单段文本 → 指定声音的音频（异步）。"""
    v = db.get(Voice, body.voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    if not body.text.strip():
        raise HTTPException(400, "文本为空")
    engine = get_engine(v.source)

    def _run(update):
        out_dir = STORAGE["drafts"] / f"tts_{uuid.uuid4().hex[:8]}"
        out_dir.mkdir(parents=True, exist_ok=True)
        raw = out_dir / "out.mp3"
        result = engine.tts(v, body.text, str(raw), update)
        rel = Path(result["path"]).relative_to(STORAGE["drafts"].parent).as_posix()
        return {"path": rel, "cost": result.get("cost", 0.0), "chars": result.get("chars", 0),
                "fallback_sentences": result.get("fallback_sentences") or [],
                "degraded_sentences": result.get("degraded_sentences") or [],
                "missing_words": result.get("missing_words") or {},
                "verified": result.get("verify_mode", "off") != "off",
                "verify_mode": result.get("verify_mode", "off"),
                "note": asr_check.format_note(result)}

    job_id = enqueue("tts", f"{body.title}（{v.name}）", _run, gpu=(v.source in ("gpt_sovits", "openvino")))
    return {"job_id": job_id}


@router.post("/synthesis/vc")
def voice_convert(body: VCBody, db: Session = Depends(get_db)):
    """初版音频 → 克隆音色（音色转换，GPT-SoVITS 本地执行）。"""
    v = db.get(Voice, body.voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    src = _resolve_audio_path(body.audio_path)
    if not src:
        raise HTTPException(404, "音频不存在")

    def _run(update):
        engine = get_engine(v.source)
        out_dir = STORAGE["drafts"] / f"vc_{uuid.uuid4().hex[:8]}"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = engine.voice_convert(v, str(src), str(out_dir / "out.mp3"), update)
        rel = Path(result["path"]).relative_to(STORAGE["drafts"].parent).as_posix()
        return {"path": rel, "cost": result.get("cost", 0.0)}

    job_id = enqueue("vc", f"{body.title}（{v.name}）", _run, gpu=True)
    return {"job_id": job_id}


def _resolve_audio_path(rel: str) -> Path | None:
    """把 'kind/xxx.wav' 相对路径解析为绝对路径（防穿越）。"""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] not in _SERVE_DIRS:
        return None
    base = _SERVE_DIRS[parts[0]].resolve()
    p = (base / "/".join(parts[1:])).resolve()
    return p if p.is_relative_to(base) and p.exists() else None


@router.get("/audio/{kind}/{file_path:path}/peaks")
def audio_peaks(kind: str, file_path: str):
    """波形峰值（前端波形图用，带缓存）。注意：需注册在 serve 之前，避免被贪婪路径吞掉。"""
    p = _resolve_audio_path(f"{kind}/{file_path}")
    if not p:
        raise HTTPException(404, "音频不存在")
    cache = STORAGE["cache"] / f"{kind}_{Path(file_path).stem}.json"
    if cache.exists() and cache.stat().st_mtime >= p.stat().st_mtime:
        import json

        return json.loads(cache.read_text(encoding="utf-8"))
    peaks = waveform_peaks(p)
    cache.write_text(__import__("json").dumps(peaks), encoding="utf-8")
    return peaks


@router.get("/audio/{kind}/{file_path:path}")
def serve_audio(kind: str, file_path: str):
    """在线播放音频（支持 Range 拖动播放）。"""
    p = _resolve_audio_path(f"{kind}/{file_path}")
    if not p:
        raise HTTPException(404, "音频不存在")
    ext = p.suffix.lower()
    return FileResponse(str(p), media_type=_MEDIA.get(ext, "application/octet-stream"))
