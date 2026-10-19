"""歌曲分离路由：上传歌曲 → 词曲分离（人声/伴奏）→ 播放下载。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from ..audio_utils import audio_duration
from ..config import ALLOWED_AUDIO_EXT, STORAGE
from ..jobs import enqueue
from ..separation import list_engines, separate

router = APIRouter(prefix="/api/separation", tags=["separation"])


class SepTaskBody(BaseModel):
    engine: str = "uvr5"
    model: str = "HP2_all_vocals"
    title: str = "歌曲分离"


@router.get("/engines")
def engines():
    return list_engines()


@router.post("/upload")
async def upload_song(file: UploadFile):
    """上传歌曲文件（mp3/wav/flac 等），保留原始文件名。"""
    import re

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(400, f"不支持的音频格式 {ext}")
    task_dir = STORAGE["separation"] / uuid.uuid4().hex[:10]
    task_dir.mkdir(parents=True, exist_ok=True)
    # 清洗文件名（去路径/非法字符），重名自动加序号
    raw_name = Path(file.filename or "song").name
    stem = re.sub(r'[\\/:*?"<>|]+', "_", raw_name)[:80] or "song"
    name = stem
    i = 1
    while (task_dir / name).exists():
        base, e = Path(stem).stem, Path(stem).suffix
        name = f"{base}({i}){e}"
        i += 1
    src = task_dir / name
    with open(src, "wb") as f:
        shutil.copyfileobj(file.file, f)
    dur = audio_duration(src)
    if dur < 5:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise HTTPException(400, "音频过短（<5 秒）")
    # 记录原始名（供列表显示）
    (task_dir / "meta.json").write_text(
        __import__("json").dumps({"original_name": raw_name, "title": Path(raw_name).stem}, ensure_ascii=False),
        encoding="utf-8",
    )
    rel = src.relative_to(STORAGE["separation"]).as_posix()
    return {"task_dir": task_dir.name, "song": f"separation/{rel}", "duration": round(dur, 1)}


def _find_song(d: Path) -> Path | None:
    """任务目录里的歌曲文件（排除分离结果）。"""
    for p in d.iterdir():
        if p.is_file() and p.suffix.lower() in ALLOWED_AUDIO_EXT and p.name not in ("vocals.wav", "accompaniment.wav"):
            return p
    return None


@router.post("/tasks/{task_dir}/run")
def run_separation(task_dir: str, body: SepTaskBody):
    """发起分离任务（后台）。"""
    d = STORAGE["separation"] / task_dir
    if not d.exists():
        raise HTTPException(404, "任务不存在")
    src = _find_song(d)
    if not src:
        raise HTTPException(404, "歌曲文件缺失")

    def _run(update):
        result = separate(body.engine, body.model, str(src), d, update)
        result["song"] = f"separation/{task_dir}/{src.name}"
        result["duration"] = audio_duration(src)
        return result

    job_id = enqueue("separation", f"分离「{src.stem}」", _run, gpu=True)
    return {"job_id": job_id}


@router.get("/tasks")
def list_tasks(page: int = 1, size: int = 10, q: str = ""):
    """分离任务列表（分页，支持按歌曲名模糊搜索）。"""
    import json as _json

    all_items = []
    if STORAGE["separation"].exists():
        dirs = sorted(
            [d for d in STORAGE["separation"].iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for d in dirs:
            song = _find_song(d)
            vocals = (d / "vocals.wav").exists()
            acc = (d / "accompaniment.wav").exists()
            original = song.name if song else ""
            meta = d / "meta.json"
            favorite = False
            if meta.exists():
                try:
                    _meta = _json.loads(meta.read_text(encoding="utf-8"))
                    original = _meta.get("original_name", original)
                    favorite = bool(_meta.get("favorite"))
                except Exception:  # noqa: BLE001
                    pass
            # 模糊搜索：匹配文件名或原始名
            if q and q.lower() not in original.lower() and q.lower() not in (song.name if song else "").lower():
                continue
            all_items.append({
                "task_dir": d.name,
                "song": f"separation/{d.name}/{song.name}" if song else "",
                "original_name": original,
                "favorite": favorite,
                "has_result": vocals and acc,
                "duration": audio_duration(song) if song else 0,
                "created": d.stat().st_mtime,
            })
    total = len(all_items)
    page = max(1, page)
    size = max(1, min(100, size))
    start = (page - 1) * size
    return {"items": all_items[start:start + size], "total": total, "page": page, "size": size, "pages": max(1, (total + size - 1) // size)}


@router.delete("/tasks/{task_dir}")
def delete_task(task_dir: str):
    """删除分离任务（歌曲与结果全部移除）。"""
    d = STORAGE["separation"] / task_dir
    if not d.exists() or not d.is_dir():
        raise HTTPException(404, "任务不存在")
    if not d.resolve().is_relative_to(STORAGE["separation"].resolve()):
        raise HTTPException(400, "非法路径")
    size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "freed_mb": round(size / 1024 / 1024, 1)}


@router.patch("/tasks/{task_dir}/favorite")
def toggle_favorite(task_dir: str):
    """切换歌曲的喜欢/收藏标识（❤️）。"""
    import json as _json

    d = STORAGE["separation"] / task_dir
    if not d.exists() or not d.is_dir():
        raise HTTPException(404, "任务不存在")
    meta = d / "meta.json"
    data = {}
    if meta.exists():
        try:
            data = _json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    data["favorite"] = not bool(data.get("favorite"))
    meta.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"favorite": data["favorite"]}
