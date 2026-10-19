"""有声读物项目路由：分段合成（初版+换声）→ 声音轨 → 参数化混音渲染。"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import asr_check
from ..audio_utils import (
    apply_fx,
    audio_duration,
    concat_segments,
    make_silence,
    mix_voice_with_bgm,
    slice_audio,
)
from ..config import ALLOWED_AUDIO_EXT, STORAGE
from ..db import get_db
from ..engines import get_engine
from ..jobs import enqueue
from ..models import Document, Project, Segment, Voice
from ..routers.mix import DEFAULT_MIX_PARAMS
from ..text_utils import chunk_text

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    title: str = "未命名作品"
    document_id: str = ""
    voice_id: str = ""
    digital_human_id: str = ""
    ref_audio_path: str = ""
    ref_text: str = ""
    params: dict = {}


class ProjectUpdate(BaseModel):
    title: str | None = None
    voice_id: str | None = None
    document_id: str | None = None
    digital_human_id: str | None = None
    ref_audio_path: str | None = None
    ref_text: str | None = None
    params: dict | None = None


def _project_dir(project_id: str) -> Path:
    d = STORAGE["projects"] / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(p: Path) -> str:
    """返回相对 data 根目录的路径（可直接用于 /api/audio/ 播放）。"""
    return p.relative_to(STORAGE["projects"].parent).as_posix()


def _abs(rel: str) -> Path | None:
    """把 'projects/xxx/file' 相对路径解析为绝对路径（防穿越）。"""
    p = (STORAGE["projects"].parent / rel).resolve()
    return p if p.is_relative_to(STORAGE["projects"].parent.resolve()) and p.exists() else None


@router.get("")
def list_projects(db: Session = Depends(get_db)):
    rows = db.execute(select(Project).order_by(Project.updated_at.desc())).scalars().all()
    out = []
    for p in rows:
        d = p.to_dict()
        d["segment_count"] = db.execute(
            select(Segment).where(Segment.project_id == p.id)
        ).scalars().all().__len__()
        out.append(d)
    return out


@router.post("")
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    if body.document_id:
        doc = db.get(Document, body.document_id)
        if not doc:
            raise HTTPException(404, "文稿不存在")
    if body.voice_id:
        v = db.get(Voice, body.voice_id)
        if not v:
            raise HTTPException(404, "声音不存在")
    p = Project(
        title=body.title or "未命名作品",
        document_id=body.document_id or None,
        voice_id=body.voice_id or None,
        digital_human_id=body.digital_human_id or "",
        params={**DEFAULT_MIX_PARAMS, **(body.params or {})},
    )
    db.add(p)
    db.commit()
    return p.to_dict()


def _normalize_path(p: str) -> str:
    """将绝对路径转换为相对路径（兼容旧数据）。"""
    if not p:
        return p
    if p.startswith(("projects/", "voices/", "drafts/")):
        return p
    if len(p) >= 2 and p[1] == ":":
        try:
            return _rel(Path(p))
        except (ValueError, OSError):
            return p
    return p


@router.get("/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = db.execute(
        select(Segment).where(Segment.project_id == p.id).order_by(Segment.idx)
    ).scalars().all()
    d = p.to_dict()
    d["segments"] = [s.to_dict() for s in segs]
    # 兼容旧数据：将分段的绝对路径转为相对路径
    for seg in d["segments"]:
        for key in ("mixed_path", "draft_path"):
            if seg.get(key):
                seg[key] = _normalize_path(seg[key])
    # 附加信息：文稿/声音名称、数字人信息、推理参考音频
    if p.document_id:
        doc = db.get(Document, p.document_id)
        d["document_title"] = doc.title if doc else None
        d["document_polished"] = bool(doc and doc.polished)
        # 分段可能与最新文稿不一致（用户改过文稿或删过段）：只提示，不自动覆盖
        if doc and d["segments"]:
            squash = lambda s: re.sub(r"\s+", "", s or "")
            fresh = "".join(chunk_text(doc.polished or doc.content, 300))
            stored = "".join(x["text"] for x in d["segments"])
            d["split_stale"] = squash(fresh) != squash(stored)
        else:
            d["split_stale"] = bool(doc and (doc.polished or doc.content or "").strip())
    if p.voice_id:
        v = db.get(Voice, p.voice_id)
        d["voice_name"] = v.name if v else None
        d["voice_source"] = v.source if v else None
        # 查找关联的数字人：优先使用存储的 digital_human_id，否则通过 voice_id 反查
        from ..digital_human import DigitalHuman
        human = None
        if p.digital_human_id:
            human = db.get(DigitalHuman, p.digital_human_id)
        if not human:
            # 通过 voice_id 查找（兼容旧数据）
            human = db.execute(
                select(DigitalHuman).where(DigitalHuman.voice_id == p.voice_id)
            ).scalars().first()
        if human:
            d["digital_human_id"] = human.id
            d["digital_human_name"] = human.name
        # 推理参考音频信息：优先使用 Project 存储的，否则从 Voice 的 engine_config 读取（兼容旧数据）
        if p.ref_audio_path:
            d["ref_audio_name"] = p.ref_audio_path.split("/")[-1].split("\\")[-1]
            d["ref_text_preview"] = (p.ref_text or "")[:50]
        elif v and v.engine_config:
            ec = v.engine_config
            d["ref_audio_name"] = ec.get("ref_path", "").split("/")[-1].split("\\")[-1] if ec.get("ref_path") else None
            d["ref_text_preview"] = (ec.get("ref_text", "") or "")[:50]
    return d


@router.patch("/{project_id}")
def update_project(project_id: str, body: ProjectUpdate, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    for k, val in body.model_dump(exclude_unset=True).items():
        if k == "params" and val is not None:
            merged = {**DEFAULT_MIX_PARAMS, **p.params, **val}
            p.params = merged
        else:
            setattr(p, k, val)
    db.commit()
    return p.to_dict()


@router.post("/{project_id}/bgm")
async def upload_bgm(project_id: str, file: UploadFile, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(400, f"不支持的音频格式 {ext}")
    d = _project_dir(p.id)
    dst = d / f"bgm{ext}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    p.bgm_path = _rel(dst)
    db.commit()
    return {"bgm_path": p.bgm_path}


def _synth_one(voice: Voice, seg: Segment, seg_dir: Path, update=None) -> float:
    """合成单段并写回音频路径与校验说明；返回本次云费用。"""
    r = get_engine(voice.source).tts(voice, seg.text, str(seg_dir / "voice.mp3"), update)
    rel = _rel(Path(r["path"]))
    seg.draft_path = rel
    seg.mixed_path = rel
    seg.error = asr_check.format_note(r)
    return float(r.get("cost", 0.0) or 0.0)


@router.post("/{project_id}/generate")
def generate(project_id: str, resume: bool = False, db: Session = Depends(get_db)):
    """分段合成：文稿切段 → 每段生成声音（edge 直接合成 / GPT-SoVITS 初版+换声 / 云引擎克隆音色合成）。

    resume=True 时跳过已就绪且音频仍在的分段（断点续做），用于任务被中断或改完
    个别分段后补做，避免把已经听过的段全部重生成。
    """
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p.document_id or not p.voice_id:
        raise HTTPException(400, "请先选择文稿与声音")
    doc = db.get(Document, p.document_id)
    if not doc:
        raise HTTPException(400, "文稿不存在")
    voice = db.get(Voice, p.voice_id)
    if not voice:
        raise HTTPException(400, "声音不存在")
    text = doc.polished or doc.content
    if not text.strip():
        raise HTTPException(400, "文稿内容为空")
    if voice.source not in ("edge", "gpt_sovits", "minimax", "fish"):
        raise HTTPException(400, f"不支持的引擎: {voice.source}")

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            p2 = d2.get(Project, project_id)
            if p2 is None:
                raise RuntimeError("项目已被删除")
            doc = d2.get(Document, p2.document_id)
            voice = d2.get(Voice, p2.voice_id)
            if doc is None:
                raise RuntimeError("文稿不存在")
            if voice is None:
                raise RuntimeError("声音不存在")
            text = doc.polished or doc.content
            if not text.strip():
                raise RuntimeError("文稿内容为空")

            d = _project_dir(p2.id)
            segments = list(d2.execute(
                select(Segment).where(Segment.project_id == p2.id).order_by(Segment.idx)
            ).scalars().all())
            if segments:
                # 已有分段 -> 复用。分段可能被用户改过文字或删除过几段，
                # 无条件按文稿重切会把人工编辑静默冲掉（这正是「合成前无法修改分片」的根源）。
                todo = 0
                for s in segments:
                    done = (s.status == "ready" and s.mixed_path and _abs(s.mixed_path))
                    if resume and done:
                        continue
                    todo += 1
                    for f in (s.draft_path, s.mixed_path):
                        if f and Path(f).is_relative_to(d):
                            Path(f).unlink(missing_ok=True)
                    s.draft_path = s.mixed_path = ""
                    s.status = "pending"
                    s.error = ""
                d2.commit()
                if resume and todo < len(segments):
                    update(3, f"断点续做：跳过已完成的 {len(segments) - todo} 段，只补 {todo} 段")
                else:
                    update(3, f"沿用现有 {len(segments)} 个分段（要按最新文稿重切请点「重新切分」）")
            else:
                chunks = chunk_text(text, 300)
                if not chunks:
                    raise RuntimeError("文稿切分后为空")
                segments = []
                for i, c in enumerate(chunks):
                    seg = Segment(project_id=p2.id, idx=i, text=c, status="pending")
                    d2.add(seg)
                    segments.append(seg)
                d2.commit()

            p2.status = "generating"
            d2.commit()
            total_cost = 0.0
            total = len(segments)

            for i, seg in enumerate(segments):
                if seg.status == "ready" and seg.mixed_path and _abs(seg.mixed_path):
                    continue          # 断点续做：这段已经好了，别重做（重做会换种子，成品会变）
                update(int(i / total * 80), f"合成第 {i + 1}/{total} 段…")
                seg.status = "drafting"
                d2.commit()
                seg_dir = d / f"seg_{i:04d}"
                seg_dir.mkdir(parents=True, exist_ok=True)

                try:
                    total_cost += _synth_one(voice, seg, seg_dir)
                    seg.status = "ready"
                except Exception as e:  # noqa: BLE001
                    seg.status = "error"
                    seg.error = str(e)
                    d2.commit()
                    p2.status = "error"
                    p2.error = f"第 {i + 1} 段失败: {e}"
                    d2.commit()
                    raise RuntimeError(f"第 {i + 1} 段合成失败: {e}")

            update(88, "拼接声音轨…")
            voice_track = _build_voice_track(p2, segments, update)
            p2.cost = round(p2.cost + total_cost, 4)
            p2.status = "voices_ready"
            p2.voice_track_path = voice_track
            p2.error = ""
            p2.chapters = auto_chapters(segments)
            # 声音轨已经变了，旧成品就是过期产物。不作废的话，⑤ 区会继续播放/下载
            # 改文字之前的那一份，用户读到的就是"改了字却不生效"。
            stale_final = bool(p2.final_path)
            if stale_final:
                p2.final_path = ""
                p2.duration = 0.0
            d2.commit()
            update(100, f"声音轨就绪（{total} 段，云费用 ¥{p2.cost:.4f}）"
                        + ("；旧成品已作废，请重新渲染" if stale_final else ""))
            return {"segments": total, "voice_track": voice_track, "cost": p2.cost,
                    "final_invalidated": stale_final}
        finally:
            d2.close()

    job_id = enqueue("project_generate", f"生成「{p.title}」声音轨", _run, gpu=True)
    p.status = "generating"
    db.commit()
    return {"job_id": job_id}


_CHAPTER_RE = re.compile(r"^\s*(第[0-9一二三四五六七八九十百千万零两]+[章回节卷部]|Chapter\s+\d+|CHAPTER\s+\d+)")


def auto_chapters(segments: list[Segment]) -> list[dict]:
    """自动切分章节：优先识别「第X章/回」等标题；没有标题时每 10 段一组。"""
    if not segments:
        return []
    chapters: list[dict] = []
    current_start = 0
    current_title = segments[0].text.strip()[:20] or f"第 1 章"
    last_ready = len(segments) - 1
    for i, seg in enumerate(segments):
        m = _CHAPTER_RE.match(seg.text.strip())
        if m and i > current_start:
            chapters.append({"title": current_title, "start_seg": current_start, "end_seg": i - 1})
            current_start = i
            current_title = seg.text.strip()[:24] or f"第 {len(chapters) + 1} 章"
    if not chapters:
        # 无章节标题：每 10 段一组
        group = 10
        for i in range(0, last_ready + 1, group):
            chapters.append({
                "title": f"第 {len(chapters) + 1} 章",
                "start_seg": i,
                "end_seg": min(i + group - 1, last_ready),
            })
    else:
        chapters.append({"title": current_title, "start_seg": current_start, "end_seg": last_ready})
    return chapters


def _build_voice_track(p: Project, segments: list[Segment], update) -> str:
    """把各段声音按间隔拼接为整条声音轨（未加 FX/背景乐，供实时预览），并记录各段起始时间。"""
    d = _project_dir(p.id)
    gap = float(p.params.get("gap", 0.6))
    files: list[str] = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        if seg.status != "ready" or not seg.mixed_path:
            continue
        seg_path = _abs(seg.mixed_path) or (d / seg.mixed_path)
        seg.offset = cursor
        files.append(str(seg_path))
        cursor += audio_duration(str(seg_path))
        if i < len(segments) - 1 and gap > 0.05:
            sil = d / f"gap_{i:04d}.wav"
            make_silence(sil, gap)
            files.append(str(sil))
            cursor += gap
    if not files:
        raise RuntimeError("没有可用的声音分段")
    out = d / "voice_track.wav"
    concat_segments(files, out, fmt="wav")
    return _rel(out)


@router.post("/{project_id}/render")
def render(project_id: str, db: Session = Depends(get_db)):
    """按混音参数渲染最终成品（语速/音调/音量/EQ/淡入淡出/背景乐/闪避）。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status not in ("voices_ready", "ready"):
        raise HTTPException(400, "请先生成声音轨（状态: " + p.status + "）")

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            p2 = d2.get(Project, project_id)
            if p2 is None:
                raise RuntimeError("项目已被删除")
            d = _project_dir(p2.id)
            params = {**DEFAULT_MIX_PARAMS, **p2.params}
            bgm = _abs(p2.bgm_path) if p2.bgm_path else None
            segments = d2.execute(
                select(Segment).where(Segment.project_id == p2.id).order_by(Segment.idx)
            ).scalars().all()

            update(15, "处理分段级参数…")
            # 1) 分段级 FX（speed/pitch/volume/eq）逐段生效
            seg_files: list[str] = []
            gap = float(params.get("gap", 0.6))
            for i, seg in enumerate(segments):
                if seg.status != "ready" or not seg.mixed_path:
                    continue
                seg_path = str(_abs(seg.mixed_path) or (d / seg.mixed_path))
                if seg.params:
                    fx = d / f"seg_fx_{i:04d}.wav"
                    apply_fx(seg_path, fx, seg.params, allow_fade=False)
                    seg_files.append(str(fx))
                else:
                    seg_files.append(seg_path)
                if i < len(segments) - 1 and gap > 0.05:
                    sil = d / f"gap_{i:04d}.wav"
                    make_silence(sil, gap)
                    seg_files.append(str(sil))

            update(40, "应用全局人声效果…")
            # 2) 拼接 → 全局 FX（不含淡入淡出）
            concat = d / "render_voice.wav"
            concat_segments(seg_files, concat, fmt="wav")
            global_fx = {k: v for k, v in params.items() if k not in ("fade_in", "fade_out")}
            global_fx["duration"] = audio_duration(concat)
            voiced = d / "render_voice_fx.wav"
            apply_fx(concat, voiced, global_fx, allow_fade=True)

            update(60, "混合背景音乐…")
            # 3) 淡入淡出 + 背景乐混音
            final = d / "final.mp3"
            fade_params = {
                "fade_in": params.get("fade_in", 0.0),
                "fade_out": params.get("fade_out", 0.0),
                "duration": audio_duration(voiced),
                "bgm_volume": params.get("bgm_volume", 0.25),
                "ducking": params.get("ducking", 0.0),
                "ducking_mode": params.get("ducking_mode", "off"),
                "ducking_curve": params.get("ducking_curve", []),
            }
            mix_voice_with_bgm(voiced, bgm, fade_params, final, fmt="mp3")
            p2.final_path = _rel(final)
            p2.duration = audio_duration(final)
            p2.status = "ready"
            p2.error = ""
            d2.commit()
            update(100, "成品渲染完成")
            return {"final_path": p2.final_path, "duration": p2.duration}
        finally:
            d2.close()

    job_id = enqueue("project_render", f"渲染「{p.title}」成品", _run)
    p.status = "rendering"
    db.commit()
    return {"job_id": job_id}


@router.get("/{project_id}/final")
def final_audio(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p or not p.final_path:
        raise HTTPException(404, "成品不存在")
    path = _abs(p.final_path)
    if not path:
        raise HTTPException(404, "成品文件缺失")
    return RedirectResponse(f"/api/audio/projects/{path.relative_to(STORAGE['projects']).as_posix()}")


# ---------- 分段级混音参数 ----------

class SegmentParamsBody(BaseModel):
    params: dict | None = None
    text: str | None = None


@router.patch("/{project_id}/segments/{idx}")
def update_segment_params(project_id: str, idx: int, body: SegmentParamsBody, db: Session = Depends(get_db)):
    """设置某分段的独立混音参数（speed/pitch/volume/eq）和/或修改该段文字。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    seg = db.execute(
        select(Segment).where(Segment.project_id == p.id, Segment.idx == idx)
    ).scalars().first()
    if not seg:
        raise HTTPException(404, "分段不存在")
    allowed = {"speed", "pitch", "volume", "eq"}
    if body.params is not None:
        clean = {k: v for k, v in body.params.items() if k in allowed}
        seg.params = clean or {}
    if body.text is not None:
        t = body.text.strip()
        if not t:
            raise HTTPException(400, "分段文字不能为空；要删掉这一段请用删除按钮")
        seg.text = t
        # 文字变了，旧音频作废，避免听到改字前的版本
        d = _project_dir(p.id)
        for f in (seg.draft_path, seg.mixed_path):
            if f and Path(f).is_relative_to(d):
                Path(f).unlink(missing_ok=True)
        seg.draft_path = seg.mixed_path = ""
        seg.status = "pending"
        seg.error = ""
    db.commit()
    return seg.to_dict()


@router.delete("/{project_id}/segments/{idx}/params")
def clear_segment_params(project_id: str, idx: int, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    seg = db.execute(
        select(Segment).where(Segment.project_id == p.id, Segment.idx == idx)
    ).scalars().first()
    if not seg:
        raise HTTPException(404, "分段不存在")
    seg.params = {}
    db.commit()
    return seg.to_dict()


# ---------- 分段切分 / 删除（合成前人工核对） ----------

def _drop_files(db: Session, p: Project, rows: list[Segment]) -> None:
    d = _project_dir(p.id)
    for s in rows:
        for f in (s.draft_path, s.mixed_path):
            if f and Path(f).is_relative_to(d):
                Path(f).unlink(missing_ok=True)
        db.delete(s)
    db.commit()


@router.post("/{project_id}/segments/split")
def split_segments(project_id: str, db: Session = Depends(get_db)):
    """按当前文稿切分分段并落库（覆盖已有分段及其音频）。

    与「生成声音轨」解耦：先切分、让用户逐条核对文字，再决定合成。
    """
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p.document_id:
        raise HTTPException(400, "请先选择文稿")
    doc = db.get(Document, p.document_id)
    if not doc:
        raise HTTPException(400, "文稿不存在")
    text = doc.polished or doc.content
    if not text.strip():
        raise HTTPException(400, "文稿内容为空")
    _drop_files(db, p, list(db.execute(
        select(Segment).where(Segment.project_id == p.id)).scalars().all()))
    chunks = chunk_text(text, 300)
    if not chunks:
        raise HTTPException(400, "切分结果为空（文稿可能只含标点）")
    for i, c in enumerate(chunks):
        db.add(Segment(project_id=p.id, idx=i, text=c, status="pending"))
    db.commit()
    return _segments_of(db, p)


@router.delete("/{project_id}/segments/{idx}")
def delete_segment(project_id: str, idx: int, db: Session = Depends(get_db)):
    """删除某个分段（该段不再合成），其余分段序号自动前移。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    seg = db.execute(
        select(Segment).where(Segment.project_id == p.id, Segment.idx == idx)
    ).scalars().first()
    if not seg:
        raise HTTPException(404, "分段不存在")
    _drop_files(db, p, [seg])
    rest = list(db.execute(
        select(Segment).where(Segment.project_id == p.id).order_by(Segment.idx)
    ).scalars().all())
    for i, s in enumerate(rest):
        s.idx = i
    db.commit()
    return _segments_of(db, p)


@router.post("/{project_id}/segments/{idx}/resynth")
def resynth_segment(project_id: str, idx: int, db: Session = Depends(get_db)):
    """只重新合成某一段，其余段的音频与起始时间不动，然后重拼声音轨。

    用于「改完这一句的文字」或「看到 ⚠ 降级告警」之后：不必为了一句话重跑整条轨
    （整跑一遍会让其它段的音频因随机种子而变化，等于推翻已听过的版本）。
    """
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    seg = db.execute(
        select(Segment).where(Segment.project_id == p.id, Segment.idx == idx)
    ).scalars().first()
    if not seg:
        raise HTTPException(404, "分段不存在")
    if not (seg.text or "").strip():
        raise HTTPException(400, "该段文字为空，无法合成")
    if not p.voice_id:
        raise HTTPException(400, "请先选择声音")
    voice = db.get(Voice, p.voice_id)
    if not voice:
        raise HTTPException(400, "声音不存在")

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            p2 = d2.get(Project, project_id)
            if p2 is None:
                raise RuntimeError("项目已被删除")
            v2 = d2.get(Voice, p2.voice_id)
            if v2 is None:
                raise RuntimeError("声音不存在")
            s2 = d2.execute(
                select(Segment).where(Segment.project_id == p2.id, Segment.idx == idx)
            ).scalars().first()
            if s2 is None:
                raise RuntimeError("分段已被删除")
            segs = list(d2.execute(
                select(Segment).where(Segment.project_id == p2.id).order_by(Segment.idx)
            ).scalars().all())
            seg_dir = _project_dir(p2.id) / f"seg_{idx:04d}"
            seg_dir.mkdir(parents=True, exist_ok=True)
            update(10, f"重新合成第 {idx + 1} 段…")
            s2.status = "drafting"
            s2.error = ""
            d2.commit()
            cost = _synth_one(v2, s2, seg_dir)
            s2.status = "ready"
            d2.commit()

            update(70, "重拼声音轨…")
            vt = _build_voice_track(p2, segs, update)
            p2.voice_track_path = vt
            p2.cost = round((p2.cost or 0.0) + cost, 4)
            p2.status = "voices_ready"
            p2.error = ""
            d2.commit()
            update(100, f"第 {idx + 1} 段已重合成" + (f"：{s2.error}" if s2.error else "，时长正常"))
            return {"segment": idx, "note": s2.error, "voice_track": vt}
        finally:
            d2.close()

    job_id = enqueue("segment_resynth", f"重合成「{p.title}」第 {idx + 1} 段", _run,
                     gpu=(voice.source in ("gpt_sovits", "openvino")))
    return {"job_id": job_id}


def _segments_of(db: Session, p: Project) -> dict:
    segs = db.execute(
        select(Segment).where(Segment.project_id == p.id).order_by(Segment.idx)
    ).scalars().all()
    return {"count": len(segs), "segments": [s.to_dict() for s in segs]}


# ---------- 章节书签 ----------

class ChaptersBody(BaseModel):
    chapters: list[dict]


def _chapter_times(segments: list[Segment]) -> list[dict]:
    """按分段偏移计算章节起止时间。"""
    out = []
    for seg in segments:
        if seg.status != "ready" or not seg.mixed_path:
            continue
        out.append({
            "idx": seg.idx,
            "offset": seg.offset,
            "duration": audio_duration(seg.mixed_path),
            "text": seg.text,
        })
    return out


@router.get("/{project_id}/chapters")
def get_chapters(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = db.execute(
        select(Segment).where(Segment.project_id == p.id).order_by(Segment.idx)
    ).scalars().all()
    times = {s["idx"]: s for s in _chapter_times(segs)}
    chapters = []
    for c in (p.chapters or []):
        start_seg, end_seg = int(c.get("start_seg", 0)), int(c.get("end_seg", 0))
        st = times.get(start_seg, {}).get("offset", 0.0)
        en_seg = times.get(end_seg, {})
        et = en_seg.get("offset", st) + en_seg.get("duration", 0.0)
        chapters.append({
            "title": c.get("title", "未命名章节"),
            "start_seg": start_seg,
            "end_seg": end_seg,
            "start": round(st, 3),
            "end": round(et, 3),
        })
    return chapters


@router.post("/{project_id}/chapters")
def save_chapters(project_id: str, body: ChaptersBody, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = db.execute(
        select(Segment).where(Segment.project_id == p.id).order_by(Segment.idx)
    ).scalars().all()
    max_idx = max((s.idx for s in segs), default=0)
    clean = []
    for c in body.chapters:
        try:
            cs, ce = int(c.get("start_seg", 0)), int(c.get("end_seg", 0))
        except (TypeError, ValueError):
            continue
        cs, ce = max(0, cs), min(max_idx, ce)
        if cs > ce:
            cs, ce = ce, cs
        clean.append({"title": str(c.get("title", "未命名章节"))[:60], "start_seg": cs, "end_seg": ce})
    p.chapters = clean
    db.commit()
    return get_chapters(project_id, db)


class ExportBody(BaseModel):
    format: str = "cue"   # cue / lrc / json
    split: bool = False   # 是否同时导出分章音频（zip）


@router.post("/{project_id}/chapters/export")
def export_chapters(project_id: str, body: ExportBody, db: Session = Depends(get_db)):
    return export_chapters_impl(project_id, body, db)


def ts(sec: float, ms: bool = True) -> str:
    m = int(sec // 60)
    s = int(sec % 60)
    frac = int(round((sec - m * 60 - s) * 1000))
    return f"{m:02d}:{s:02d}.{frac:03d}" if ms else f"{m:02d}:{s:02d}"


def export_chapters_impl(project_id: str, body: ExportBody, db: Session):
    """导出章节书签：cue/lrc/json 索引，split=True 时打包分章 mp3 为 zip。"""
    import zipfile

    from fastapi.responses import FileResponse

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    chapters = get_chapters(project_id, db)
    if not chapters:
        raise HTTPException(400, "暂无章节，请先生成声音轨")
    src = _abs(p.final_path) if p.final_path else None
    if not src or not src.exists():
        src = _abs(p.voice_track_path) if p.voice_track_path else None
    if not src or not src.exists():
        raise HTTPException(400, "没有可导出的音频（请先生成声音轨或渲染成品）")

    d = _project_dir(p.id)
    fmt = body.format if body.format in ("cue", "lrc", "json") else "cue"
    safe = re.sub(r'[\\/:*?"<>|]+', "_", p.title or "作品")[:40]
    stem = safe

    if body.split:
        zip_path = d / f"{stem}_分章.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            total = audio_duration(src)
            for i, ch in enumerate(chapters):
                st, et = ch["start"], min(ch["end"], total)
                if et - st < 0.05:
                    continue
                part = d / f"ch_{i + 1:02d}.mp3"
                slice_audio(src, part, st, et, fmt="mp3")
                zf.write(part, f"{stem}_第{i + 1:02d}章.mp3")
                part.unlink(missing_ok=True)
            # 索引导入包
            if fmt == "cue":
                zf.writestr(f"{stem}.cue", _build_cue(p.title, chapters))
            elif fmt == "lrc":
                zf.writestr(f"{stem}.lrc", _build_lrc(chapters))
            else:
                zf.writestr(f"{stem}.json", json.dumps(chapters, ensure_ascii=False, indent=2))
        return {"ok": True, "file": _rel(zip_path), "chapters": len(chapters)}

    if fmt == "cue":
        out = d / f"{stem}.cue"
        out.write_text(_build_cue(p.title, chapters), encoding="utf-8")
    elif fmt == "lrc":
        out = d / f"{stem}.lrc"
        out.write_text(_build_lrc(chapters), encoding="utf-8")
    else:
        out = d / f"{stem}.json"
        out.write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(str(out), filename=out.name)


def _build_cue(title: str, chapters: list[dict]) -> str:
    def idx(sec: float) -> str:
        m = int(sec // 60)
        s = int(sec % 60)
        f = int(round((sec - m * 60 - s) * 75))  # CUE 帧：75 帧/秒
        return f"{m:02d}:{s:02d}:{f:02d}"

    lines = [
        'REM GENRE "有声读物"',
        f'TITLE "{title}"',
        'FILE "audio.mp3" MP3',
    ]
    for i, ch in enumerate(chapters):
        lines.append(f'  TRACK {i + 1:02d} AUDIO')
        lines.append(f'    TITLE "{ch["title"]}"')
        lines.append(f'    INDEX 01 {idx(ch["start"])}')
    return "\n".join(lines) + "\n"


def _build_lrc(chapters: list[dict]) -> str:
    lines = [f"[ti:{ch['title']}]" for ch in chapters[:1]] or []
    for ch in chapters:
        lines.append(f"[{ts(ch['start'])}]{ch['title']}")
    return "\n".join(lines) + "\n"


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = db.execute(select(Segment).where(Segment.project_id == p.id)).scalars().all()
    for s in segs:
        db.delete(s)
    shutil.rmtree(STORAGE["projects"] / p.id, ignore_errors=True)
    db.delete(p)
    db.commit()
    return {"ok": True}
