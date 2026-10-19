"""音频分片路由：上传长音频 → 自动分片 + 自动标注 → 逐条校对 → 试听/下载/导出。

刻意与数字人训练数据流程分开：这里不依赖数字人、不写 train.list 到训练目录，
产物都留在 data/slicing/<任务id>/ 下，可独立下载或导出。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import STORAGE
from ..db import get_db
from ..jobs import enqueue
from ..slicing import (DEFAULTS, AudioSlice, SliceTask, align_units, asr_label_batch, asr_one,
                       build_export, cut_groups, cut_piece, group_units, media_duration,
                       silence_ranges, slice_by_silence, snap_cuts)

router = APIRouter(prefix="/api/slicing", tags=["slicing"])


# ---------------- 目录与校验 ----------------

def _task_dir(task_id: str) -> Path:
    d = STORAGE["slicing"] / task_id
    if not d.is_dir() or not d.resolve().is_relative_to(STORAGE["slicing"].resolve()):
        raise HTTPException(404, "任务不存在")
    return d


def _slices_dir(task_id: str) -> Path:
    d = _task_dir(task_id) / "slices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_of(db: Session, slice_id: str) -> tuple[SliceTask, AudioSlice, Path]:
    s = db.get(AudioSlice, slice_id)
    if not s:
        raise HTTPException(404, "片段不存在")
    t = db.get(SliceTask, s.task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = _task_dir(t.id)
    p = (d / "slices" / Path(s.name).name).resolve()
    if not p.is_file() or not p.is_relative_to(d):
        raise HTTPException(404, "片段音频文件缺失")
    return t, s, p


def _clean(name: str, fallback: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())[:80]
    return stem or fallback


def _refresh_task(db: Session, t: SliceTask) -> None:
    """重算启用片段的序号与任务统计，保证列表 / 导出 / 打包三者一致。

    软删除的行只改序号、不删记录也不删文件 —— 删除必须可反悔。
    """
    rows = db.execute(
        select(AudioSlice).where(AudioSlice.task_id == t.id, AudioSlice.is_deleted == 0)
        .order_by(AudioSlice.start, AudioSlice.id)
    ).scalars().all()
    for i, s in enumerate(rows):
        s.idx = i
    t.slice_count = len(rows)
    t.labeled_count = sum(1 for s in rows if (s.text or "").strip())
    t.total_sec = round(sum(s.duration for s in rows), 2)
    db.commit()


# ---------------- 任务：建 / 跑 / 列 / 详情 / 删 ----------------

@router.post("/upload")
async def upload(file: UploadFile = File(...), title: str = ""):
    """上传一份长音频，建立一个分片任务（还没切，等 /process）。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".mp4"):
        raise HTTPException(400, f"不支持的音频格式 {ext}")
    tid = uuid.uuid4().hex[:10]
    d = STORAGE["slicing"] / tid
    (d / "source").mkdir(parents=True, exist_ok=True)
    raw_name = Path(file.filename or "audio").name
    src = d / "source" / f"{_clean(Path(raw_name).stem, 'audio')}{ext}"
    with open(src, "wb") as f:
        shutil.copyfileobj(file.file, f)
    dur = media_duration(src)
    if dur < 3:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "音频过短（<3 秒），无需分片")
    t = SliceTask(id=tid, title=_clean(title or Path(raw_name).stem, "分片任务"),
                  source_name=raw_name, duration=round(dur, 2), status="ready",
                  params=dict(DEFAULTS))
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        db.add(t)
        db.commit()
    finally:
        db.close()
    return {"task_id": tid, "title": t.title, "duration": round(dur, 2)}


class ProcessBody(BaseModel):
    noise_db: float | None = None
    min_silence: float | None = None
    min_piece: float | None = None
    max_piece: float | None = None
    merge_gap: float | None = None
    redo: bool = False          # True=清掉旧片段重新切；False=只在后面追加
    mode: str = "silence"       # silence=按静音切 | align=识别对齐（句子/词绝不被劈开）
    granularity: str = "para"   # align 时：para=按段（若干完整句）| sentence=按句
    max_chars: int = 300        # align 段模式：一段最多多少字
    max_sec: float = 30.0       # align 段模式：一段最长多少秒
    hotwords: str | None = None  # align 时的热词（空格/逗号分隔的专名表）


@router.post("/{task_id}/process")
def process(task_id: str, body: ProcessBody, db: Session = Depends(get_db)):
    """后台执行「切片 → ASR 标注」，进度走 /api/jobs/{job_id}。"""
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    srcs = sorted((_task_dir(task_id) / "source").glob("*"))
    if not srcs:
        raise HTTPException(400, "源音频缺失，请重新上传")
    src = srcs[0]
    params = {**DEFAULTS, **(t.params or {})}
    for k in ("noise_db", "min_silence", "min_piece", "max_piece", "merge_gap",
              "granularity", "max_chars", "max_sec"):
        v = getattr(body, k)
        if v is not None:
            params[k] = v
    params["mode"] = body.mode if body.mode in ("silence", "align") else "silence"
    if params["granularity"] not in ("para", "sentence"):
        raise HTTPException(400, "granularity 只能是 para（按段）或 sentence（按句）")
    if not 30 <= int(params["max_chars"]) <= 2000:
        raise HTTPException(400, "max_chars 应在 30~2000 字之间")
    if not 3 <= float(params["max_sec"]) <= 120:
        raise HTTPException(400, "max_sec 应在 3~120 秒之间")
    if params["min_piece"] < 0.2 or params["min_piece"] > 10:
        raise HTTPException(400, "min_piece 应在 0.2~10 秒之间")
    if 0 < params["max_piece"] < params["min_piece"]:
        raise HTTPException(400, "max_piece 不能小于 min_piece")
    if not -60 <= params["noise_db"] <= -15:
        raise HTTPException(400, "noise_db 应在 -60~-15 dB 之间")
    t.params = params
    if body.hotwords is not None:
        t.hotwords = (body.hotwords or "").strip()[:2000]
    t.status = "slicing"
    t.error = ""
    db.commit()

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            t2 = d2.get(SliceTask, task_id)
            d = _task_dir(task_id)
            sdir = d / "slices"
            start_index = 1
            if body.redo:
                shutil.rmtree(sdir, ignore_errors=True)
                d2.query(AudioSlice).filter(AudioSlice.task_id == task_id).delete()
                d2.commit()
            else:
                start_index = (d2.get(SliceTask, task_id).slice_count or 0) + 1
                sdir.mkdir(parents=True, exist_ok=True)
            if params.get("mode") == "align":
                total = media_duration(src)
                hw = (t2.hotwords or "").strip()
                update(4, "整条音频识别（带时间戳）…")
                cache = d / "sentences.json"
                units, info = None, {}
                if cache.exists():
                    try:
                        info = json.loads(cache.read_text(encoding="utf-8"))
                    except Exception:  # 缓存坏了就重识别
                        info = {}
                    # 热词变了必须重识别，否则会沿用没有偏置的旧文本
                    if info.get("units") and (info.get("hotwords") or "").strip() == hw:
                        units = info["units"]
                        update(30, "复用上次识别结果（%d 个标点单元）…" % len(units))
                if not units:
                    units = align_units(src, cache, hw)
                    info = json.loads(cache.read_text(encoding="utf-8"))
                update(55, "按句子边界归片…")
                groups = group_units(units, params.get("granularity") or "para",
                                     int(params.get("max_chars") or 300),
                                     float(params.get("max_sec") or 30.0),
                                     float(params.get("min_piece") or 1.0))
                groups = snap_cuts(groups, silence_ranges(src, float(params["noise_db"]),
                                                           float(params["min_silence"])), total)
                pieces = cut_groups(src, sdir, groups, start_index=start_index, progress=update)
                if not pieces:
                    raise RuntimeError("识别对齐模式没切出任何片段（源音频可能没有人声）")
                for p in pieces:
                    d2.add(AudioSlice(task_id=task_id, name=p.name, text=(p.text or "")[:2000],
                                      start=p.start, end=p.end, duration=p.duration, flags=p.flags))
                d2.commit()
                _refresh_task(d2, t2)
                flagged = sum(1 for p in pieces if p.flags)
                note = "识别对齐：%d 个标点单元 → %d 片，覆盖 %.1f%%" % (
                    len(units), len(pieces), 100 * sum(p.duration for p in pieces) / total
                    if total else 0.0)
                model = str(info.get("model") or "")
                if hw:
                    note += "；热词 %d 个（%s）" % (len(hw.split()), "SeACo" if "seaco" in model else "模型不支持热词")
                    if "seaco" not in model:
                        note += "！实际用的是 %s，它不吃热词，热词没生效 —— 需下载 SeACo-Paraformer" % (model or "默认模型")
                if flagged:
                    note += "；%d 片边界可疑（引号未闭合或无句末标点），请优先试听" % flagged
                t2.note = note
                t2.status = "done"
                t2.finished_at = datetime.now()
                d2.commit()
                update(100, "完成：%d 段，已标注 %d 段" % (t2.slice_count, t2.labeled_count))
                return {"slices": t2.slice_count, "labeled": t2.labeled_count,
                        "total_sec": t2.total_sec, "note": t2.note}

            update(3, "按静音切片中…")
            pieces = slice_by_silence(src, sdir, params, start_index=start_index, progress=update)
            if not pieces:
                raise RuntimeError("没有切出任何片段，请调低 noise_db 或缩短 min_piece")
            # 先把片段入库（文本留空），ASR 期间用户就能看到结果；中途取消也不丢已切好的
            for p in pieces:
                d2.add(AudioSlice(task_id=task_id, name=p.name, text="",
                                  start=p.start, end=p.end, duration=p.duration))
            d2.commit()
            _refresh_task(d2, t2)
            from ..jobs import is_cancelled
            if is_cancelled(job_id):
                t2.status = "done"
                t2.note = "已按要求停止，未做 ASR 标注（可点「重标空白」补标）"
                t2.finished_at = datetime.now()
                d2.commit()
                update(100, f"已切出 {t2.slice_count} 段（未标注）")
                return {"slices": t2.slice_count, "labeled": 0,
                        "total_sec": t2.total_sec, "note": t2.note}
            t2.status = "labeling"
            d2.commit()
            update(60, f"共 {len(pieces)} 段，开始自动标注…")
            texts, note = asr_label_batch([p.path for p in pieces], d / "_asr", t2.lang or "zh")
            for s in d2.execute(
                select(AudioSlice).where(AudioSlice.task_id == task_id)).scalars().all():
                v = texts.get(s.name, "")
                if v and not (s.text or "").strip():
                    s.text = v[:2000]
            d2.commit()
            t2.note = note or ""
            _refresh_task(d2, t2)
            t2.status = "done"
            t2.finished_at = datetime.now()
            d2.commit()
            update(100, f"完成：{t2.slice_count} 段，已标注 {t2.labeled_count} 段")
            return {"slices": t2.slice_count, "labeled": t2.labeled_count,
                    "total_sec": t2.total_sec, "note": t2.note}
        except Exception as e:  # noqa: BLE001
            t3 = d2.get(SliceTask, task_id)
            if t3:
                t3.status = "error"
                t3.error = str(e)[:500]
                d2.commit()
            raise
        finally:
            d2.close()

    job_id = enqueue("slicing", f"分片「{t.title}」", _run, gpu=True)
    return {"job_id": job_id, "slices_dir": f"slicing/{task_id}/slices"}


@router.get("")
def list_tasks(page: int = 1, size: int = 12, q: str = ""):
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        stmt = select(SliceTask).order_by(SliceTask.created_at.desc())
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(SliceTask.title.like(like), SliceTask.source_name.like(like)))
        items = [t.to_dict() for t in db.execute(stmt).scalars().all()]
    finally:
        db.close()
    total = len(items)
    page, size = max(1, page), max(1, min(100, size))
    st = (page - 1) * size
    return {"items": items[st:st + size], "total": total, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size)}


@router.get("/{task_id}")
def task_detail(task_id: str, db: Session = Depends(get_db)):
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = t.to_dict()
    d["dirs"] = {"slices": f"slicing/{task_id}/slices"}
    pend = db.execute(
        select(func.count()).select_from(AudioSlice).where(
            AudioSlice.task_id == task_id, AudioSlice.is_deleted == 0, AudioSlice.text == "")
    ).scalar() or 0
    d["unlabeled_count"] = pend
    d["defaults"] = DEFAULTS
    return d


class TaskUpdate(BaseModel):
    title: str | None = None
    lang: str | None = None
    hotwords: str | None = None


@router.patch("/{task_id}")
def update_task(task_id: str, body: TaskUpdate, db: Session = Depends(get_db)):
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if body.title is not None:
        t.title = _clean(body.title, t.title)
    if body.lang:
        t.lang = body.lang.strip()[:10]
    if body.hotwords is not None:
        t.hotwords = (body.hotwords or "").strip()[:2000]
    db.commit()
    return t.to_dict()


@router.delete("/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db)):
    d = _task_dir(task_id)
    size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d, ignore_errors=True)
    db.query(AudioSlice).filter(AudioSlice.task_id == task_id).delete()
    db.query(SliceTask).filter(SliceTask.id == task_id).delete()
    db.commit()
    return {"ok": True, "freed_mb": round(size / 1048576, 1)}


# ---------------- 片段：列 / 改 / 删 / 恢复 / 拆分 ----------------

@router.get("/{task_id}/slices")
def list_slices(task_id: str, page: int = 1, size: int = 50, q: str = "",
                only_unlabeled: bool = False, deleted_only: bool = False,
                db: Session = Depends(get_db)):
    """片段列表。`deleted_only=1` 用来查看已删除的片段并恢复（软删除必须可反悔）。"""
    if not db.get(SliceTask, task_id):
        raise HTTPException(404, "任务不存在")
    stmt = select(AudioSlice).where(
        AudioSlice.task_id == task_id,
        AudioSlice.is_deleted == (1 if deleted_only else 0),
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(AudioSlice.text.like(like), AudioSlice.name.like(like)))
    rows = db.execute(stmt.order_by(AudioSlice.idx, AudioSlice.start)).scalars().all()
    if only_unlabeled and not deleted_only:
        rows = [r for r in rows if not (r.text or "").strip()]
    total = len(rows)
    page, size = max(1, page), max(1, min(500, size))
    st = (page - 1) * size
    return {"items": [r.to_dict() for r in rows[st:st + size]], "total": total,
            "page": page, "size": size, "pages": max(1, (total + size - 1) // size)}


class SliceUpdate(BaseModel):
    text: str


@router.patch("/slices/{slice_id}")
def update_slice(slice_id: str, body: SliceUpdate, db: Session = Depends(get_db)):
    s = db.get(AudioSlice, slice_id)
    if not s:
        raise HTTPException(404, "片段不存在")
    s.text = (body.text or "").strip()[:2000]
    s.edited = 1
    db.commit()
    t = db.get(SliceTask, s.task_id)
    if t:
        t.labeled_count = db.execute(
            select(func.count()).select_from(AudioSlice).where(
                AudioSlice.task_id == t.id, AudioSlice.is_deleted == 0,
                AudioSlice.text != "")).scalar() or 0
        db.commit()
    return s.to_dict()


@router.delete("/slices/{slice_id}")
def delete_slice(slice_id: str, db: Session = Depends(get_db)):
    """软删除：只从导出/打包/列表里去掉，磁盘文件保留，可反悔恢复。"""
    s = db.get(AudioSlice, slice_id)
    if not s:
        raise HTTPException(404, "片段不存在")
    s.is_deleted = 1
    db.commit()
    t = db.get(SliceTask, s.task_id)
    if t:
        _refresh_task(db, t)
    return {"ok": True, "id": slice_id}


@router.post("/slices/{slice_id}/restore")
def restore_slice(slice_id: str, db: Session = Depends(get_db)):
    s = db.get(AudioSlice, slice_id)
    if not s:
        raise HTTPException(404, "片段不存在")
    s.is_deleted = 0
    db.commit()
    t = db.get(SliceTask, s.task_id)
    if t:
        _refresh_task(db, t)
    return s.to_dict()


class SplitBody(BaseModel):
    at: float                       # 在距本片段开头多少秒处一分为二
    retext: bool = True             # 是否对两片重新 ASR


@router.post("/slices/{slice_id}/split")
def split_slice(slice_id: str, body: SplitBody, db: Session = Depends(get_db)):
    """把一个片段在指定秒处切成两段（边界切坏了时的补救手段）。"""
    t, s, src = _file_of(db, slice_id)
    if body.at <= 0.05 or body.at >= s.duration - 0.05:
        raise HTTPException(400, f"切点必须在 0.05~{s.duration - 0.05:.2f} 秒之间")
    sdir = src.parent
    base = Path(s.name).stem
    left, right = sdir / f"{base}_L.wav", sdir / f"{base}_R.wav"
    if not cut_piece(src, left, 0.0, body.at) or not cut_piece(src, right, body.at, s.duration):
        left.unlink(missing_ok=True)
        right.unlink(missing_ok=True)
        raise HTTPException(400, "切分失败（ffmpeg 返回异常）")
    dl, dr = media_duration(left), media_duration(right)
    old_text = (s.text or "").strip()
    # 文本先按字数比例粗分，勾选 retext 时用 ASR 覆盖成准确内容
    cut_frac = body.at / s.duration if s.duration else 0.5
    k = int(round(len(old_text) * cut_frac))
    tl, tr = (old_text[:k], old_text[k:]) if old_text else ("", "")
    if body.retext:
        tl = asr_one(left) or tl
        tr = asr_one(right) or tr
    s.is_deleted = 1
    db.add(AudioSlice(task_id=t.id, name=left.name, text=tl.strip(), start=s.start,
                      end=round(s.start + body.at, 3), duration=round(dl, 3)))
    db.add(AudioSlice(task_id=t.id, name=right.name, text=tr.strip(), start=round(s.start + body.at, 3),
                      end=s.end, duration=round(dr, 3)))
    db.commit()
    # 母片段只软删：文件留在原处，恢复时还能用，也不会被「只删不改」的语义悄悄弄丢
    _refresh_task(db, t)
    return {"ok": True, "from": s.name, "into": [left.name, right.name]}


@router.post("/{task_id}/retext")
def retext_all(task_id: str, only_empty: bool = True, db: Session = Depends(get_db)):
    """批量重标：默认只补空白标注的那几片（ASR 偶发漏识别时用）。"""
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = _task_dir(task_id)
    rows = db.execute(
        select(AudioSlice).where(AudioSlice.task_id == task_id, AudioSlice.is_deleted == 0)
        .order_by(AudioSlice.idx)).scalars().all()
    todo = [r for r in rows if not (r.text or "").strip()] if only_empty else list(rows)
    if not todo:
        return {"updated": 0, "total": len(rows)}
    texts, note = asr_label_batch([d / "slices" / r.name for r in todo], d / "_asr_retext", t.lang or "zh")
    n = 0
    for r in todo:
        v = texts.get(r.name, "")
        if v:
            r.text = v[:2000]
            r.edited = 0
            n += 1
    db.commit()
    _refresh_task(db, t)
    return {"updated": n, "total": len(todo), "note": note}


# ---------------- 导入为数字人训练数据 ----------------

class ImportBody(BaseModel):
    human_id: str = ""          # 导入到已有数字人
    human_name: str = ""        # human_id 为空时用这个名字新建数字人
    source: str = "main"        # main / s1 / s2
    mode: str = "append"        # append=只补新增；overwrite=本任务导入过的重新复制并刷新文本


@router.post("/{task_id}/import")
def import_to_human(task_id: str, body: ImportBody, db: Session = Depends(get_db)):
    """把分片任务的片段+标注导入成数字人的训练数据。

    分片任务导出的是 slices/*.wav + train.list，而训练器按 train.list 里的裸文件名去找
    <data_dir>/<name>.wav 或 <data_dir>/sliced/<name>.wav（gpt_sovits_train._stage_labeled_slices）。
    直接把任务目录指过去会让「训练数据」页把这些 wav 登记成空文本记录，之后任何一次
    删除/追加都会用数据库重写 train.list（humans.py），人工标注静默丢失。
    所以这里是**复制进训练目录 + 文本写进 TrainingSlice**，让界面和训练器读同一份数据。
    """
    from ..config import DATA_DIR
    from ..digital_human import DigitalHuman, INFERENCE_PRESETS
    from ..training_slice import TrainingSlice
    from .humans import _nd, _source_dir

    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if body.mode not in ("append", "overwrite"):
        raise HTTPException(400, "mode 只能是 append 或 overwrite")
    rows = db.execute(
        select(AudioSlice).where(AudioSlice.task_id == task_id, AudioSlice.is_deleted == 0)
        .order_by(AudioSlice.idx, AudioSlice.start)).scalars().all()
    if not rows:
        raise HTTPException(400, "这个任务还没有可用的片段")
    todo = [r for r in rows if (r.text or "").strip()]
    unlabeled = len(rows) - len(todo)
    if not todo:
        raise HTTPException(400, "全部片段都没有标注文本：训练需要音频与文字成对，"
                                "请先逐条补标或点「重标空白」")

    if body.human_id:
        h = db.get(DigitalHuman, body.human_id)
        if not h:
            raise HTTPException(404, "数字人不存在")
    elif (body.human_name or "").strip():
        h = DigitalHuman(name=body.human_name.strip()[:100],
                         desc="由音频分片任务导入训练数据",
                         inference_params=INFERENCE_PRESETS["文学朗读"])
        db.add(h)
        db.commit()
    else:
        raise HTTPException(400, "请选择数字人，或填一个新数字人名字")

    if (body.source or "main") == "main" and not h.data_dir:
        h.data_dir = _nd(DATA_DIR / "training_data" / h.id)
        db.commit()
    d = Path(_source_dir(db, h, body.source or "main"))
    cur = _nd(d)

    src_dir = _task_dir(task_id) / "slices"
    # 本目录内已存在的记录（含别的批次）：同名一律让路，绝不覆盖别人的训练数据
    active = {}
    for s in db.execute(select(TrainingSlice).where(
            TrainingSlice.data_dir == cur, TrainingSlice.is_deleted == 0)).scalars().all():
        active.setdefault(s.audio, s)
    # 本任务此前导入过的记录，按文件名索引
    tagged = {s.audio: s for s in db.execute(select(TrainingSlice).where(
        TrainingSlice.slice_task_id == task_id, TrainingSlice.data_dir == cur,
        TrainingSlice.is_deleted == 0)).scalars().all()}
    orders = db.execute(select(TrainingSlice.sort_order).where(
        TrainingSlice.data_dir == cur)).scalars().all()
    nxt = (max(orders) + 1) if orders else 0

    def dest_name(r: AudioSlice) -> str:
        if r.name in tagged:
            return r.name
        taken = r.name in active or (d / r.name).exists()
        if not taken:
            return r.name
        # 后缀用片段自身 id，重跑导入时算出的名字不变，不会堆出重复记录
        return "%s_%s%s" % (Path(r.name).stem, r.id[:6], Path(r.name).suffix)

    copied = refreshed = kept = renamed = missing = 0
    for r in todo:
        src = src_dir / r.name
        if not src.is_file():
            missing += 1
            continue
        name = dest_name(r)
        mine = tagged.get(name)
        if mine is not None and body.mode == "append":
            kept += 1
            continue
        shutil.copy2(src, d / name)
        if name != r.name:
            renamed += 1
        text = (r.text or "").strip()[:2000]
        if mine is None:
            s = TrainingSlice(human_id=h.id, audio=name, text=text, lang=t.lang or "zh",
                              category="other", sort_order=nxt, data_dir=cur,
                              slice_task_id=task_id)
            db.add(s)
            db.flush()
            nxt += 1
            copied += 1
            tagged[name] = s
            active[name] = s
        else:
            # 覆盖模式下文本没变也算「原样保留」，否则明明重导了 8 段却报「导入 0 段」
            if (mine.text or "") != text:
                refreshed += 1
            else:
                kept += 1
            mine.text, mine.lang, mine.is_deleted = text, t.lang or "zh", 0
    db.commit()

    # train.list 与「训练数据」页同源重建：此后用户在训练页删改也不会再丢掉这些标注
    final = db.execute(
        select(TrainingSlice).where(
            TrainingSlice.human_id == h.id, TrainingSlice.is_deleted == 0,
            or_(TrainingSlice.data_dir == cur, TrainingSlice.data_dir == "")).order_by(
            TrainingSlice.sort_order)).scalars().all()
    lines = [f"{s.audio}|speaker0|{s.lang or 'zh'}|{(s.text or '').strip()}" for s in final]
    (d / "train.list").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    total_sec = round(sum(r.duration for r in todo), 1)
    msg = f"已导入 {copied + refreshed + kept} 段到「{h.name}」"
    if copied:
        msg += f"（新增 {copied}"
        msg += f"、标注更新 {refreshed}）" if refreshed else "）"
    elif refreshed:
        msg += f"（标注更新 {refreshed}）"
    if kept:
        msg += f"，{kept} 段原样保留"
    if renamed:
        msg += f"，{renamed} 段因重名换了文件名"
    if unlabeled:
        msg += f"，{unlabeled} 段无标注文本未导入"
    if missing:
        msg += f"，{missing} 段音频文件缺失"
    return {
        "ok": True, "human_id": h.id, "human_name": h.name, "source": body.source or "main",
        "mode": body.mode, "target_dir": str(d), "train_list_lines": len(lines),
        "copied": copied, "refreshed": refreshed, "kept": kept, "renamed": renamed,
        "missing_audio": missing, "skipped_unlabeled": unlabeled,
        "total_sec": total_sec, "message": msg,
    }


# ---------------- 播放 / 下载 / 导出 ----------------

@router.get("/slices/{slice_id}/audio")
def slice_audio(slice_id: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse

    _, _, p = _file_of(db, slice_id)
    return FileResponse(p, media_type="audio/wav", filename=p.name)


@router.get("/slices/{slice_id}/download")
def slice_download(slice_id: str, db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse

    _, s, p = _file_of(db, slice_id)
    return FileResponse(p, media_type="audio/wav", filename=s.name)


@router.get("/{task_id}/download")
def task_download(task_id: str, db: Session = Depends(get_db)):
    """打包全部启用片段：wav + 同名 .txt + train.list + slices.jsonl，一次下走。"""
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = _task_dir(task_id)
    rows = db.execute(
        select(AudioSlice).where(AudioSlice.task_id == task_id, AudioSlice.is_deleted == 0)
        .order_by(AudioSlice.idx)).scalars().all()
    if not rows:
        raise HTTPException(400, "没有可下载的片段（可能都被删了）")
    out = d / f"导出_{t.title}_{len(rows)}段_{time.strftime('%m%d%H%M')}.zip"
    tmp = out.with_suffix(".tmp.zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for s in rows:
            p = d / "slices" / s.name
            if p.is_file():
                z.write(p, f"slices/{s.name}")
                if (s.text or "").strip():
                    z.writestr(f"texts/{Path(s.name).stem}.txt", s.text.strip() + "\n")
        build_export(rows, tmp.parent / "_train.list.tmp", "list", t.lang or "zh")
        z.write(tmp.parent / "_train.list.tmp", "train.list")
        (tmp.parent / "_train.list.tmp").unlink(missing_ok=True)
        build_export(rows, tmp.parent / "_slices.jsonl.tmp", "jsonl", t.lang or "zh")
        z.write(tmp.parent / "_slices.jsonl.tmp", "slices.jsonl")
        (tmp.parent / "_slices.jsonl.tmp").unlink(missing_ok=True)
        z.writestr("说明.txt", f"源音频：{t.source_name}\n片段数：{len(rows)}\n"
                              f"合计时长：{round(sum(s.duration for s in rows), 1)} 秒\n"
                              f"train.list 为 GPT-SoVITS 训练格式：文件名|speaker0|语言|文本\n")
    if out.exists():
        out.unlink()
    tmp.rename(out)
    from fastapi.responses import FileResponse

    return FileResponse(out, media_type="application/zip", filename=out.name)


@router.get("/{task_id}/export")
def task_export(task_id: str, fmt: str = "list", db: Session = Depends(get_db)):
    """只导出标注：fmt=list（train.list 格式）| jsonl。"""
    t = db.get(SliceTask, task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if fmt not in ("list", "jsonl"):
        raise HTTPException(400, "fmt 只能是 list 或 jsonl")
    d = _task_dir(task_id)
    rows = db.execute(
        select(AudioSlice).where(AudioSlice.task_id == task_id, AudioSlice.is_deleted == 0)
        .order_by(AudioSlice.idx)).scalars().all()
    if not rows:
        raise HTTPException(400, "没有可导出的片段")
    name = f"{'train.list' if fmt == 'list' else 'slices.jsonl'}"
    out = d / f"_导出_{name}"
    build_export(rows, out, fmt, t.lang or "zh")
    from fastapi.responses import FileResponse

    return FileResponse(out, media_type="text/plain; charset=utf-8", filename=name)


@router.get("/{task_id}/stats")
def task_stats(task_id: str, db: Session = Depends(get_db)):
    """体积统计，供界面上「删除前提示」。"""
    d = _task_dir(task_id)
    files = [p for p in d.rglob("*") if p.is_file()]
    return {"files": len(files), "mb": round(sum(p.stat().st_size for p in files) / 1048576, 1)}
