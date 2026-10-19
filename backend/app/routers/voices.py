"""声音库路由：创建/克隆/管理声音模型。"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audio_utils import audio_duration
from ..config import ALLOWED_AUDIO_EXT, STORAGE
from ..db import get_db
from ..engines import all_engines, get_engine
from ..jobs import enqueue
from ..models import Voice

router = APIRouter(prefix="/api/voices", tags=["voices"])


class VoiceCreate(BaseModel):
    name: str
    source: str = "edge"
    edge_voice: str = ""
    ref_text: str = ""
    ref_lang: str = "zh"
    timbre: str = ""
    model_id: str = ""


class VoiceUpdate(BaseModel):
    name: str | None = None
    timbre: str | None = None
    ref_text: str | None = None
    ref_lang: str | None = None
    edge_voice: str | None = None
    engine_config: dict | None = None


def _save_ref_audio(voice: Voice, file: UploadFile) -> str:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(400, f"不支持的音频格式 {ext}，支持: {sorted(ALLOWED_AUDIO_EXT)}")
    d = STORAGE["voices"] / voice.id
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"ref{ext}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return str(dst)


@router.get("")
def list_voices(db: Session = Depends(get_db)):
    rows = db.execute(select(Voice).order_by(Voice.created_at.desc())).scalars().all()
    return [v.to_dict() for v in rows]


@router.post("")
def create_voice(body: VoiceCreate, db: Session = Depends(get_db)):
    voice = Voice(
        name=body.name or "未命名声音",
        source=body.source,
        edge_voice=body.edge_voice,
        ref_text=body.ref_text,
        ref_lang=body.ref_lang or "zh",
        timbre=body.timbre,
        model_id=body.model_id,
    )
    db.add(voice)
    db.commit()
    return voice.to_dict()


@router.get("/{voice_id}")
def get_voice(voice_id: str, db: Session = Depends(get_db)):
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    return v.to_dict()


@router.patch("/{voice_id}")
def update_voice(voice_id: str, body: VoiceUpdate, db: Session = Depends(get_db)):
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    for k, val in body.model_dump(exclude_unset=True).items():
        if k == "engine_config" and isinstance(val, dict):
            # 合并而非整体替换：前端更新参考音频时只传 ref_* 字段，
            # 整体替换会清掉 gpt_model/sovits_model 等微调模型绑定
            v.engine_config = {**(v.engine_config or {}), **val}
        else:
            setattr(v, k, val)
    db.commit()
    return v.to_dict()


@router.post("/{voice_id}/ref-audio")
async def upload_ref_audio(voice_id: str, file: UploadFile, db: Session = Depends(get_db)):
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    path = _save_ref_audio(v, file)
    v.ref_audio = path
    v.duration = audio_duration(path)
    if v.duration < 1:
        raise HTTPException(400, "音频文件无效或过短")
    v.status = "draft"
    db.commit()
    return {"ref_audio": path, "duration": v.duration}


@router.post("/{voice_id}/clone")
def clone_voice(voice_id: str, db: Session = Depends(get_db)):
    """克隆/训练声音（异步任务）。edge 音色无需克隆。"""
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    if v.source == "edge":
        v.status = "ready"
        db.commit()
        return {"job_id": None, "voice": v.to_dict()}

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            v2 = d2.get(Voice, voice_id)
            if v2 is None:
                raise RuntimeError("声音已被删除")
            engine = get_engine(v2.source)
            result = engine.clone(v2, d2, update)
            v2.model_id = result.get("model_id", v2.model_id)
            v2.engine_config = result.get("engine_config", v2.engine_config)
            v2.status = "ready"
            v2.error = ""
            d2.commit()
            return result
        finally:
            d2.close()

    job_id = enqueue("voice_clone", f"克隆声音「{v.name}」", _run, gpu=(v.source == "gpt_sovits"))
    v.status = "training"
    db.commit()
    return {"job_id": job_id}


@router.delete("/{voice_id}")
def delete_voice(voice_id: str, db: Session = Depends(get_db)):
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    d = STORAGE["voices"] / v.id
    shutil.rmtree(d, ignore_errors=True)
    db.delete(v)
    db.commit()
    return {"ok": True}


@router.post("/{voice_id}/train-audio")
async def upload_train_audio(voice_id: str, file: UploadFile, db: Session = Depends(get_db)):
    """上传训练音频（长音频或多段音频；支持 zip 打包多个文件）。"""
    import zipfile

    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    train_dir = STORAGE["voices"] / v.id / "train_data"
    train_dir.mkdir(parents=True, exist_ok=True)
    name = file.filename or "train"
    if name.lower().endswith(".zip"):
        zpath = train_dir / "upload.zip"
        with open(zpath, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(train_dir)
        zpath.unlink(missing_ok=True)
    else:
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXT:
            raise HTTPException(400, f"不支持的音频格式 {ext}，支持: {sorted(ALLOWED_AUDIO_EXT)}，或上传 zip 打包")
        with open(train_dir / f"train_{len(list(train_dir.glob('*.*')))}{ext}", "wb") as f:
            shutil.copyfileobj(file.file, f)
    # 统计音频
    audios = [p for p in train_dir.rglob("*") if p.suffix.lower() in ALLOWED_AUDIO_EXT]
    total_sec = sum(audio_duration(p) for p in audios)
    return {"files": len(audios), "duration": round(total_sec, 1)}


class TrainBody(BaseModel):
    speaker: str = "speaker0"   # 说话人标记（名人音频建议用不同标记，实现音色融合）
    s2_epochs: int = 10
    s1_epochs: int = 10


@router.post("/{voice_id}/train")
def train_voice(voice_id: str, body: TrainBody, db: Session = Depends(get_db)):
    """微调训练：上传的音频 → 切片 → ASR 标注 → 预处理 → s2+s1 训练 → 声音模型。"""
    v = db.get(Voice, voice_id)
    if not v:
        raise HTTPException(404, "声音不存在")
    if v.source != "gpt_sovits":
        raise HTTPException(400, "微调训练仅支持 GPT-SoVITS 声音")
    train_dir = STORAGE["voices"] / v.id / "train_data"
    audios = [p for p in train_dir.rglob("*") if p.suffix.lower() in ALLOWED_AUDIO_EXT]
    if not audios:
        raise HTTPException(400, "请先上传训练音频（声音详情 → 训练数据）")
    total_sec = sum(audio_duration(p) for p in audios)
    if total_sec < 30:
        raise HTTPException(400, f"训练音频合计仅 {total_sec:.0f} 秒，建议至少 1~3 分钟（几十条短句效果最佳）")
    s2 = max(2, min(100, body.s2_epochs))
    s1 = max(2, min(100, body.s1_epochs))

    def _run(update):
        from ..db import SessionLocal
        from ..gpt_sovits_train import GPTSoVITSTrainer

        d2 = SessionLocal()
        try:
            v2 = d2.get(Voice, voice_id)
            if v2 is None:
                raise RuntimeError("声音已被删除")
            trainer = GPTSoVITSTrainer(f"{v2.id}_{v2.name}", update)
            result = trainer.run(train_dir, body.speaker or "speaker0", s2, s1)
            # 记录微调模型到声音配置
            v2.engine_config = dict(v2.engine_config or {})
            v2.engine_config["gpt_model"] = result["gpt_model"]
            v2.engine_config["sovits_model"] = result["sovits_model"]
            v2.engine_config["finetuned"] = True
            v2.engine_config["train_exp"] = result["exp_dir"]
            d2.commit()
            update(100, "微调训练完成！声音模型已就绪")
            return result
        finally:
            d2.close()

    job_id = enqueue("voice_train", f"微调训练「{v.name}」", _run, gpu=True)
    return {"job_id": job_id}


@router.get("/engines/info")
def engine_info(db: Session = Depends(get_db)):
    """所有引擎可用性 + 预设音色（供前端选择）。"""
    out = []
    for e in all_engines():
        info = e.check(db)
        out.append({
            "id": e.id,
            "name": e.name,
            "desc": e.desc,
            "supports_clone": e.supports_clone,
            "supports_vc": e.supports_vc,
            "is_cloud": e.is_cloud,
            "cost_hint": e.cost_hint,
            "ok": info["ok"],
            "check_message": info["message"],
            "check_detail": info["detail"],
            "preset_voices": e.list_voices(db),
        })
    return out
