"""混音路由：参数化渲染（服务端精确版，与前端实时预览参数一致）。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..audio_utils import audio_duration, mix_voice_with_bgm, waveform_peaks
from ..config import STORAGE
from ..db import get_db
from ..jobs import enqueue

router = APIRouter(prefix="/api/mix", tags=["mix"])

# 默认混音参数（前端滑块与之对应）
DEFAULT_MIX_PARAMS = {
    "speed": 1.0,          # 语速 0.7~1.6
    "pitch": 0,            # 音调（半音 -6~+6）
    "volume": 1.0,         # 人声音量 0~2
    "eq": {"low": 0, "mid": 0, "high": 0},  # 三段 EQ dB -12~+12
    "fade_in": 0.0,        # 淡入秒
    "fade_out": 0.0,       # 淡出秒
    "gap": 0.6,            # 段落间隔秒
    "bgm_volume": 0.25,    # 背景乐音量 0~1
    "ducking": 0.0,        # 侧链闪避强度 0~1
    "ducking_mode": "off", # off / sidechain / curve
    "ducking_curve": [     # 自定义闪避曲线关键帧（pct: 位置百分比, level: 闪避深度）
        {"pct": 0, "level": 0.3},
        {"pct": 25, "level": 0.3},
        {"pct": 50, "level": 0.3},
        {"pct": 75, "level": 0.3},
        {"pct": 100, "level": 0.3},
    ],
}


class MixBody(BaseModel):
    voice_path: str          # 相对路径 drafts/xxx.wav
    bgm_path: str = ""       # 相对路径，可为空
    params: dict = {}


def _resolve(rel: str) -> Path | None:
    from .synthesis import _resolve_audio_path

    return _resolve_audio_path(rel)


@router.post("/render")
def render_mix(body: MixBody):
    """把 人声+背景乐 按参数渲染为成品音频（异步任务）。"""
    voice = _resolve(body.voice_path)
    if not voice:
        raise HTTPException(404, "人声音频不存在")
    bgm = _resolve(body.bgm_path) if body.bgm_path else None
    if body.bgm_path and not bgm:
        raise HTTPException(404, "背景乐不存在")

    params = {**DEFAULT_MIX_PARAMS, **(body.params or {})}
    params["duration"] = audio_duration(voice)

    def _run(update):
        out_dir = STORAGE["drafts"] / f"mix_{uuid.uuid4().hex[:8]}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "mixed.mp3"
        update(30, "混音渲染中…")
        mix_voice_with_bgm(voice, bgm, params, out)
        update(100, "完成")
        return {"path": f"drafts/{out.parent.name}/mixed.mp3"}

    job_id = enqueue("mix", "混音渲染", _run)
    return {"job_id": job_id}


@router.get("/defaults")
def mix_defaults():
    return DEFAULT_MIX_PARAMS
