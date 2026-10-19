"""音频润色路由：上传朗诵初稿 → 参数化渲染（EQ/混响/压缩等）→ 播放下载。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from ..audio_polish import render_polish
from ..audio_utils import audio_duration
from ..config import ALLOWED_AUDIO_EXT, STORAGE
from ..jobs import enqueue

router = APIRouter(prefix="/api/polish", tags=["polish"])

# 默认润色参数（前端滑块与之对应）
DEFAULT_POLISH_PARAMS = {
    "eq": {"low": 0, "mid_low": 0, "mid": 0, "mid_high": 0, "high": 0},
    "highpass": 0,
    "lowpass": 0,
    "reverb_amount": 0,
    "reverb_decay": 1.5,
    "compressor": {"enabled": False, "threshold": -20, "ratio": 3, "attack": 10, "release": 200, "makeup": 3},
    "noise_reduction": 0,
    "deesser": 0,
    "loudness": 0,
}


class PolishBody(BaseModel):
    params: dict = {}
    title: str = "音频润色"


@router.post("/upload")
async def upload_polish(file: UploadFile):
    """上传朗诵初稿音频。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(400, f"不支持的音频格式 {ext}")
    d = STORAGE["polish"] / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    raw_name = Path(file.filename or "draft").name
    src = d / f"draft{ext}"
    with open(src, "wb") as f:
        shutil.copyfileobj(file.file, f)
    dur = audio_duration(src)
    if dur < 3:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "音频过短（<3 秒）")
    (d / "meta.json").write_text(
        __import__("json").dumps({"name": raw_name, "duration": round(dur, 1)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"task_dir": d.name, "duration": round(dur, 1), "name": raw_name}


@router.post("/{task_dir}/render")
def render(task_dir: str, body: PolishBody):
    """按参数渲染润色音频（后台任务）。"""
    d = STORAGE["polish"] / task_dir
    if not d.exists():
        raise HTTPException(404, "任务不存在")
    drafts = [p for p in d.glob("draft.*") if p.suffix.lower() in ALLOWED_AUDIO_EXT]
    if not drafts:
        raise HTTPException(404, "初稿缺失")
    src = drafts[0]
    params = {**DEFAULT_POLISH_PARAMS, **body.params}

    def _run(update):
        out = d / "polished.mp3"
        render_polish(src, params, out)
        update(100, "润色完成")
        return {"path": f"polish/{task_dir}/polished.mp3", "duration": audio_duration(out)}

    job_id = enqueue("polish", f"润色「{body.title}」", _run)
    return {"job_id": job_id}


@router.get("/tasks")
def list_tasks():
    """润色任务列表。"""
    import json as _json

    out = []
    if STORAGE["polish"].exists():
        for d in sorted(STORAGE["polish"].iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            if not d.is_dir():
                continue
            drafts = [p for p in d.glob("draft.*") if p.suffix.lower() in ALLOWED_AUDIO_EXT]
            polished = (d / "polished.mp3").exists()
            name = drafts[0].name if drafts else "?"
            meta = d / "meta.json"
            if meta.exists():
                try:
                    name = _json.loads(meta.read_text(encoding="utf-8")).get("name", name)
                except Exception:  # noqa: BLE001
                    pass
            out.append({
                "task_dir": d.name,
                "name": name,
                "draft": f"polish/{d.name}/{drafts[0].name}" if drafts else "",
                "has_result": polished,
                "polished": f"polish/{d.name}/polished.mp3" if polished else "",
                "duration": audio_duration(drafts[0]) if drafts else 0,
                "created": d.stat().st_mtime,
            })
    return out
