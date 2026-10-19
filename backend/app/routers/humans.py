"""数字人路由：创建/管理数字人、训练、推理。"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import or_, select

from ..config import STORAGE, ALLOWED_AUDIO_EXT
from ..audio_utils import audio_duration
from ..db import get_db
from ..digital_human import DigitalHuman, INFERENCE_PRESETS, TRAINING_PRESETS_8GB, analyze_training_data
from ..training_record import TrainingRecord, scan_data_hashes, check_duplicates
from ..models import Voice, AIModel, get_active_model
from ..slicing import asr_label_batch, run_slice_pipeline

router = APIRouter(prefix="/api/humans", tags=["humans"])


def _nd(p) -> str:
    """归一化目录路径，用于存储与比较。

    Windows 路径大小写不敏感（磁盘扫描能找到文件），但 SQLite 的文本 `=` 区分大小写，
    两者混用会造成"音频列出来、标注匹配不上"。统一在写入与比较时都过一遍 normcase。
    """
    return os.path.normcase(str(p)) if p else ""


class HumanCreate(BaseModel):
    name: str = "我的数字人"
    voice_id: str = ""
    desc: str = ""


class HumanUpdate(BaseModel):
    name: str | None = None
    desc: str | None = None
    voice_id: str | None = None
    data_dir: str | None = None
    s1_data_dir: str | None = None
    s2_data_dir: str | None = None
    model_s2: str | None = None
    model_gpt: str | None = None
    status: str | None = None
    polish_params: dict | None = None
    training_config: dict | None = None
    inference_params: dict | None = None


@router.get("")
def list_humans(db: Session = Depends(get_db)):
    rows = db.execute(select(DigitalHuman).order_by(DigitalHuman.updated_at.desc())).scalars().all()
    result = []
    for h in rows:
        d = h.to_dict()
        # 补充关联信息
        if h.voice_id:
            v = db.get(Voice, h.voice_id)
            d["voice_name"] = v.name if v else "已删除"
        else:
            d["voice_name"] = "未绑定"
        result.append(d)
    return result


@router.post("")
def create_human(body: HumanCreate, db: Session = Depends(get_db)):
    h = DigitalHuman(
        name=body.name or "我的数字人",
        desc=body.desc,
        voice_id=body.voice_id or None,
        inference_params=INFERENCE_PRESETS["文学朗读"],
    )
    db.add(h)
    db.commit()
    return h.to_dict()


@router.get("/{human_id}")
def get_human(human_id: str, db: Session = Depends(get_db)):
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    d = h.to_dict()
    if h.voice_id:
        v = db.get(Voice, h.voice_id)
        d["voice_name"] = v.name if v else "已删除"
    # 补充数据统计
    if h.data_dir:
        d["data_stats"] = analyze_training_data(h.data_dir)
    return d


@router.patch("/{human_id}")
def update_human(human_id: str, body: HumanUpdate, db: Session = Depends(get_db)):
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None:
            if k.endswith("data_dir"):
                v = _nd(v)
            setattr(h, k, v)
    db.commit()
    return h.to_dict()


@router.delete("/{human_id}")
def delete_human(human_id: str, db: Session = Depends(get_db)):
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    db.delete(h)
    db.commit()
    return {"ok": True}


@router.get("/presets/training")
def training_presets():
    """8GB GPU 专属训练参数预设。"""
    return TRAINING_PRESETS_8GB


@router.get("/presets/inference")
def inference_presets():
    """推理参数预设（文学朗读/稳定朗读/生动演绎）。"""
    return INFERENCE_PRESETS


@router.get("/presets/models")
def available_models(db: Session = Depends(get_db)):
    """获取可用的训练基础模型列表（官方预训练 + 历史训练模型）。"""
    # 官方预训练模型
    models = [{"id": "official_gsv-v2", "name": "GPT-SoVITS v2 官方预训练", "type": "official"}]
    # 历史训练模型
    records = db.execute(
        select(TrainingRecord).where(TrainingRecord.status == "success").order_by(TrainingRecord.finished_at.desc())
    ).scalars().all()
    seen = set()
    for r in records:
        if r.model_s2 and r.model_s2 not in seen:
            models.append({
                "id": r.model_s2,
                "name": f"训练模型 ({r.created_at.strftime('%m-%d %H:%M') if r.created_at else ''})",
                "type": "trained",
                "data_dir": r.data_dir,
                "epoch_info": r.params.get("sovits", {}).get("epochs", "?"),
            })
            seen.add(r.model_s2)
    return models


@router.get("/{human_id}/history")
def training_history(human_id: str, db: Session = Depends(get_db)):
    """获取数字人的训练历史。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    records = db.execute(
        select(TrainingRecord).where(TrainingRecord.human_id == human_id)
        .order_by(TrainingRecord.created_at.desc())
    ).scalars().all()
    result = []
    for r in records:
        d = r.to_dict()
        d["duplication_rate"] = round(len(r.data_hash or []) / max(1, len(r.data_hash or [])), 2)
        result.append(d)
    return result


@router.get("/{human_id}/check-duplicates")
def check_data_duplicates(human_id: str, db: Session = Depends(get_db)):
    """检查当前数据与历史训练数据的重复情况。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    # 当前数据哈希
    new_hashes = scan_data_hashes(h.data_dir)
    # 历史训练数据哈希
    records = db.execute(
        select(TrainingRecord).where(
            TrainingRecord.human_id == human_id,
            TrainingRecord.status == "success",
        )
    ).scalars().all()
    history_hashes = []
    for r in records:
        history_hashes.extend(r.data_hash or [])
    dup = check_duplicates(new_hashes, history_hashes)
    dup["record_count"] = len(records)
    dup["current_files"] = len(new_hashes)
    return dup


def _source_dir(db, h, source: str) -> str:
    """解析 source(main/s1/s2) 对应的数据目录；s1/s2 首次使用自动创建并回写字段。

    - main: h.data_dir（主数据）
    - s1/s2: h.s1_data_dir / h.s2_data_dir（独立目录，为空时基于主目录派生并创建）
    """
    from ..config import DATA_DIR

    if source not in ("main", "s1", "s2"):
        source = "main"
    if source == "main":
        if not h.data_dir:
            raise HTTPException(400, "请先设置训练数据目录")
        d = Path(h.data_dir)
    else:
        field = h.s1_data_dir if source == "s1" else h.s2_data_dir
        if field:
            d = Path(field)
        else:
            base = h.data_dir or str(DATA_DIR / "training_data" / f"{h.id}")
            d = Path(base + f"_{source}")
            if source == "s1":
                h.s1_data_dir = _nd(d)
            else:
                h.s2_data_dir = _nd(d)
            db.commit()
    d.mkdir(parents=True, exist_ok=True)
    return _nd(d)


@router.post("/{human_id}/data-upload")
async def upload_training_audio(human_id: str, file: UploadFile, mode: str = Form("replace"), source: str = Form("main"), db: Session = Depends(get_db)):
    """上传长音频或压缩包（zip/rar，系统自动解压+切片+ASR标注）。

    mode: replace=清空旧数据后重新上传（默认）；append=保留旧数据，追加新文件。
    source: main=主数据（默认）；s1=s1 语感数据；s2=s2 音色数据（各自独立目录）。
    """
    from ..config import ALLOWED_AUDIO_EXT

    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    d = Path(_source_dir(db, h, source))

    ext = Path(file.filename or "").suffix.lower()
    is_archive = ext in (".zip", ".rar", ".7z")
    if ext not in ALLOWED_AUDIO_EXT and not is_archive:
        raise HTTPException(400, f"不支持的格式 {ext}（支持音频: wav/mp3/m4a/flac，压缩包: zip/rar/7z）")

    d.mkdir(parents=True, exist_ok=True)

    up_dir = d / "uploads"
    if mode == "replace":
        # 替换语义：先清空旧数据（旧源音频/旧切片/切片目录/旧解压文件），
        # 避免新旧文件混入训练数据
        for old in d.glob("raw_*"):
            if old.is_file():
                old.unlink(missing_ok=True)
        for old in d.glob("*_seg*.wav"):
            old.unlink(missing_ok=True)
        shutil.rmtree(d / "sliced", ignore_errors=True)
        shutil.rmtree(up_dir, ignore_errors=True)
    else:
        # 追加语义：保留旧数据，新文件放入 uploads/（data-process?mode=append 只处理这里）
        up_dir.mkdir(parents=True, exist_ok=True)

    # 保存原始文件：replace 存根目录 raw_*；append 存 uploads/
    filename = file.filename or "audio.wav"
    if mode == "replace":
        if not filename.startswith("raw_"):
            filename = f"raw_{filename}"
        src = d / filename
    else:
        src = up_dir / filename
    with open(src, "wb") as f_out:
        shutil.copyfileobj(file.file, f_out)

    # 压缩包：解压到 uploads 子目录
    if is_archive:
        import zipfile

        up_dir.mkdir(parents=True, exist_ok=True)  # replace 时已删，重建；append 时已存在
        extracted = 0
        if ext == ".zip":
            with zipfile.ZipFile(src) as zf:
                for m in zf.infolist():
                    if m.filename.endswith("/"):
                        continue
                    mname = Path(m.filename).name
                    if Path(mname).suffix.lower() in ALLOWED_AUDIO_EXT:
                        target = up_dir / mname
                        with zf.open(m) as src_f, open(target, "wb") as dst_f:
                            shutil.copyfileobj(src_f, dst_f)
                        extracted += 1
        else:
            # rar/7z：尝试用系统工具解压（unrar/7z），失败则返回提示
            import subprocess as sp

            for tool, args in [
                (r"C:\Program Files\7-Zip\7z.exe", ["x", "-y", f"-o{up_dir}"]),
                (r"C:\Program Files\WinRAR\WinRAR.exe", ["x", "-y", str(up_dir)]),
            ]:
                if os.path.exists(tool):
                    try:
                        sp.run([tool, *args, str(src)], capture_output=True, timeout=120)
                        break
                    except Exception:  # noqa: BLE001
                        continue
            extracted = len([p for p in up_dir.glob("*") if p.suffix.lower() in ALLOWED_AUDIO_EXT])
        if extracted == 0:
            raise HTTPException(400, "压缩包内未找到音频文件")
        src.unlink(missing_ok=True)
        return {"file": str(up_dir), "duration": 0, "archive": True, "extracted": extracted,
                "message": f"解压完成，找到 {extracted} 个音频文件，请点击「开始切片标注」"}

    dur = audio_duration(src)
    return {"file": src.name, "duration": round(dur, 1), "archive": False}


@router.post("/{human_id}/data-process")
def process_training_data(human_id: str, mode: str = "replace", source: str = "main",
                          slice_mode: str = "align", granularity: str = "para",
                          max_chars: int = 0, max_sec: float = 0, hotwords: str = "",
                          db: Session = Depends(get_db)):
    """自动处理：逐个音频切片 → ASR 标注 → 生成可校对列表。

    mode: replace=清空旧数据重建（默认）；append=只处理 uploads/ 下的新文件，保留旧切片与标注。
    source: main/s1/s2 数据源目录。
    slice_mode: silence=按静音（快）；align=先整条识别拿标点时间戳，只在句子边界下刀，
                标注随识别一次到位（s1 语感用 granularity=para，s2 音色用 sentence）。
    """
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    d = Path(_source_dir(db, h, source))

    AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg")
    up_dir = d / "uploads"
    raw_files = []
    if mode == "append":
        # 追加：只处理 uploads/ 下的新文件（旧 raw_*/旧切片/旧标注全部保留）
        if up_dir.exists():
            for ext in AUDIO_EXT:
                raw_files.extend(up_dir.glob(f"*{ext}"))
    else:
        # 收集所有待处理音频：根目录 raw_* + uploads/ 解压出的
        for ext in AUDIO_EXT:
            raw_files.extend(d.glob(f"raw_*{ext}"))
        if up_dir.exists():
            for ext in AUDIO_EXT:
                raw_files.extend(up_dir.glob(f"*{ext}"))
    if not raw_files:
        raise HTTPException(400, "请先上传音频或压缩包（raw_*.wav/mp3/m4a 或 uploads/ 目录）")

    if mode != "append":
        # 兜底清理：清空根目录旧切片 + sliced/，确保只基于本次源文件生成全新数据
        for old in d.glob("*_seg*.wav"):
            old.unlink(missing_ok=True)
        shutil.rmtree(d / "sliced", ignore_errors=True)

    sliced_dir = d / "sliced"
    if mode == "append":
        # 追加模式：保留旧切片（旧切片的 ASR 标注源不能丢），只 mkdir
        sliced_dir.mkdir(parents=True, exist_ok=True)
    else:
        # replace 重建，sliced/ 只保留本次新切片
        shutil.rmtree(sliced_dir, ignore_errors=True)
        sliced_dir.mkdir(exist_ok=True)

    from ..training_slice import TrainingSlice

    # 切片编号：replace 从 1 开始；append 接续**文件名里已有的最大号**。
    # 不能用 sort_order 推：文件名是 1 起始、sort_order 是 0 起始，直接用后者会让追加的
    # 第一段与已有最后一段同号（旧数据里 006_raw_x_seg6 与 006_20260816_seg1 就是这么来的）。
    index = 1
    if mode == "append":
        nums = []
        for p in list(d.glob("*_seg*.wav")) + list(sliced_dir.glob("*.wav")):
            head = p.name.split("_", 1)[0]
            if head.isdigit():
                nums.append(int(head))
        index = (max(nums) + 1) if nums else 1
    # 切片算法与「音频分片」模块同源（app/slicing.py），不在这里再写一遍。
    # 旧实现有个致命缺陷：两刀之间不足 1.5 秒时它照样推进游标，等于把那段语音直接丢掉；
    # 数值口径保持与过去一致（不强制细分长段），只把「丢」改成「并」。
    slice_params = {"noise_db": -35.0, "min_silence": 0.5, "min_piece": 1.0,
                    "max_piece": 0.0, "merge_gap": 1.5,
                    "mode": slice_mode if slice_mode in ("silence", "align") else "silence",
                    "granularity": granularity if granularity in ("para", "sentence") else "para",
                    "max_chars": max(30, min(2000, int(max_chars))) if max_chars else 0,
                    "max_sec": max(3.0, min(120.0, float(max_sec))) if max_sec else 0,
                    "hotwords": (hotwords or "").strip()[:2000]}
    align_cache = d / "_align"
    new_pieces = []
    failed = []
    notes = []
    for raw in raw_files:
        try:
            pieces, note = run_slice_pipeline(
                raw, d, slice_params, start_index=index,
                cache_path=align_cache / (raw.stem + ".json"))
        except Exception as e:  # 一份文件读不动不该拖垮整批
            failed.append("%s（%s）" % (raw.name, str(e)[:50]))
            continue
        for p in pieces:
            shutil.copy2(p.path, sliced_dir / p.name)
        new_pieces.extend(pieces)
        index += len(pieces)
        if note:
            notes.append(note)

    # ---- ASR 自动标注：走分片模块同一条路径（官方 funasr_asr.py，缺失时自动降级）----
    # 只标本次新切的片段：append 模式下旧切片的文本已在数据库里，不必重识别。
    if slice_params["mode"] == "align":
        asr_texts = {p.name: (p.text or "") for p in new_pieces}   # 识别对齐自带标注
        asr_note = "；".join(notes)
    else:
        asr_texts, asr_note = asr_label_batch([p.path for p in new_pieces], d / "_asr", "zh")

    # 生成 train.list（训练用）+ 写入数据库（主数据源）
    from ..digital_human import _classify_text

    # 本次切片集合：直接用切完的 Piece 列表（append 只含新增，replace 已先清空根目录）
    wavs = [p.path for p in new_pieces]
    if mode == "replace":
        # 清理当前 data_dir 的旧切片数据（保留其他目录的记录）
        db.query(TrainingSlice).filter(
            TrainingSlice.human_id == human_id,
            TrainingSlice.data_dir == _nd(d),
        ).delete()
        start_order = 0
    else:
        # 追加：保留旧记录，新记录序号接续
        orders = db.execute(
            select(TrainingSlice.sort_order).where(
                TrainingSlice.human_id == human_id,
                TrainingSlice.is_deleted == 0,
                TrainingSlice.data_dir == _nd(d),
            )
        ).scalars().all()
        start_order = (max(orders) + 1) if orders else 0
    # 写入切片记录（标记 data_dir 与可疑 flags）
    for i, pc in enumerate(new_pieces):
        s = TrainingSlice(human_id=human_id, audio=pc.name, text=asr_texts.get(pc.name, ""),
                          lang="zh", category="other", sort_order=start_order + i,
                          data_dir=_nd(d), flags=pc.flags)
        db.add(s)
    db.commit()
    # train.list：从数据库全量生成（append 时含旧+新记录）
    rows = db.execute(
        select(TrainingSlice).where(
            TrainingSlice.human_id == human_id,
            TrainingSlice.is_deleted == 0,
            or_(TrainingSlice.data_dir == _nd(d), TrainingSlice.data_dir == ""),
        ).order_by(TrainingSlice.sort_order)
    ).scalars().all()
    list_lines = [f"{s.audio}|speaker0|zh|{s.text}" for s in rows]
    (d / "train.list").write_text("\n".join(list_lines), encoding="utf-8")

    labeled = sum(1 for w in wavs if asr_texts.get(w.name))
    if mode == "append":
        msg = f"追加完成：新增 {len(wavs)} 段（保留旧数据），ASR 标注 {labeled} 段，请逐条校对"
    elif labeled:
        msg = f"切片完成，共 {len(wavs)} 个片段，ASR 自动标注 {labeled} 段，请逐条校对"
    elif asr_note:
        msg = f"切片完成，共 {len(wavs)} 个片段，{asr_note}"
    else:
        msg = f"切片完成，共 {len(wavs)} 个片段，ASR 未识别到文字，请人工校对"

    if not wavs:
        msg = "没切出任何片段：" + ("、".join(failed) if failed else "音频可能没有可识别的人声")
    elif failed:
        msg += "；跳过读不动的文件：" + "、".join(failed)
    n_flagged = sum(1 for p in new_pieces if p.flags)
    if slice_params["mode"] == "align" and asr_note:
        msg += "；" + asr_note
    if n_flagged:
        msg += f"；{n_flagged} 段边界可疑，建议优先校对"

    return {
        "sliced_files": len(wavs),
        "list_file": "train.list",
        "asr_labeled": labeled,
        "flagged": n_flagged,
        "slice_mode": slice_params["mode"],
        "mode": mode,
        "failed": failed,
        "message": msg,
    }


@router.get("/{human_id}/data-list")
def list_training_data(human_id: str, page: int = 1, size: int = 20, source: str = "main", db: Session = Depends(get_db)):
    """列出训练数据（从数据库读取，支持分页）。"""
    from ..training_slice import TrainingSlice

    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    d = Path(_source_dir(db, h, source))
    current_dir = _nd(d)

    # 从数据库读取切片记录（排除软删除 + 去重）
    all_slices = db.execute(
        select(TrainingSlice)
        .where(TrainingSlice.human_id == human_id, TrainingSlice.is_deleted == 0)
        .order_by(TrainingSlice.sort_order)
    ).scalars().all()

    # 去重：同一音频只保留第一条
    seen = set()
    unique_slices = []
    for s in all_slices:
        if s.audio not in seen:
            seen.add(s.audio)
            unique_slices.append(s)
        else:
            s.is_deleted = 1
    if len(unique_slices) != len(all_slices):
        db.commit()

    # 按当前 data_dir 过滤（兼容旧数据：data_dir 为空的记录也显示）
    slices = [s for s in unique_slices if not s.data_dir or _nd(s.data_dir) == current_dir]

    # 扫描当前 data_dir，与已有记录合并（保留旧记录的文本标注）
    d = Path(current_dir)
    current_audio = {}  # filename -> TrainingSlice (existing or new)
    if d.exists():
        audio_files = sorted(
            f for f in d.rglob("*.wav")
            if "_seg" in f.name
        )
        # 按文件名索引已有记录
        for s in slices:
            current_audio[s.audio] = s
        # 扫描目录，已有则保留，新增则创建（标记 data_dir）
        for i, f in enumerate(audio_files):
            if f.name not in current_audio:
                s = TrainingSlice(
                    human_id=human_id, audio=f.name, text="", lang="zh",
                    category="other", sort_order=i, data_dir=current_dir,
                )
                db.add(s)
                current_audio[f.name] = s
            elif not current_audio[f.name].data_dir:
                # 旧记录补标 data_dir
                current_audio[f.name].data_dir = current_dir
        db.commit()
        # 重新查询（包含新增的）
        slices = db.execute(
            select(TrainingSlice)
            .where(TrainingSlice.human_id == human_id, TrainingSlice.is_deleted == 0,
                   or_(TrainingSlice.data_dir == current_dir, TrainingSlice.data_dir == ""))
            .order_by(TrainingSlice.sort_order)
        ).scalars().all()

    total = len(slices)

    # 分页
    page = max(1, page)
    size = max(1, min(50, size))
    start = (page - 1) * size
    page_slices = slices[start:start + size]

    results = []
    for s in page_slices:
        results.append({
            "idx": s.id,
            "audio": s.audio,
            "text": s.text or "",
            "lang": s.lang,
            "category": s.category,
            "flags": s.flags or "",
            "audio_url": f"/api/humans/{human_id}/audio/{s.audio}?source={source}",
        })

    total_chars = sum(len(s.text or "") for s in slices)
    return {
        "count": total,
        "total_chars": total_chars,
        "items": results,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
    }


@router.get("/{human_id}/audio/{file_path:path}")
def serve_training_audio(human_id: str, file_path: str, source: str = "main", db: Session = Depends(get_db)):
    """提供训练数据目录下的音频文件（在线播放）。source: main/s1/s2 对应各自独立目录。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    if source == "s1":
        base = h.s1_data_dir or h.data_dir
    elif source == "s2":
        base = h.s2_data_dir or h.data_dir
    else:
        base = h.data_dir
    if not base:
        raise HTTPException(404, "数字人不存在")
    d = Path(base).resolve()
    target = (d / file_path).resolve()
    # 路径安全：必须位于训练数据目录内
    if not target.is_relative_to(d):
        raise HTTPException(400, "非法路径")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    mime = "audio/wav"
    if target.suffix.lower() in (".mp3",):
        mime = "audio/mpeg"
    elif target.suffix.lower() in (".m4a", ".aac"):
        mime = "audio/mp4"
    elif target.suffix.lower() in (".flac",):
        mime = "audio/flac"
    elif target.suffix.lower() in (".ogg", ".opus"):
        mime = "audio/ogg"
    return FileResponse(str(target), media_type=mime)


class TextUpdate(BaseModel):
    idx: int = -1
    audio: str = ""
    text: str


@router.post("/{human_id}/data-text")
@router.post("/{human_id}/data-text")
def save_training_text(human_id: str, body: TextUpdate, source: str = "main", db: Session = Depends(get_db)):
    """保存切片标注文字（数据库操作，不再改 train.list 文件）。"""
    from ..training_slice import TrainingSlice

    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    cur_dir = _source_dir(db, h, source)

    # 按音频文件名查找（取第一条，去重处理）
    s = None
    if body.audio:
        matches = db.execute(
            select(TrainingSlice).where(
                TrainingSlice.human_id == human_id,
                TrainingSlice.audio == body.audio,
                TrainingSlice.is_deleted == 0,
                or_(TrainingSlice.data_dir == cur_dir, TrainingSlice.data_dir == ""),
            )
        ).scalars().all()
        if len(matches) > 1:
            # 清理重复：只保留第一条，其他标记为已删除
            for dup in matches[1:]:
                dup.is_deleted = 1
            db.commit()
        s = matches[0] if matches else None
    if not s and body.idx > 0:
        s = db.get(TrainingSlice, body.idx)

    if not s:
        raise HTTPException(400, "未找到对应的切片记录")

    s.text = body.text
    from ..digital_human import _classify_text
    s.category = _classify_text(body.text)
    db.commit()
    return {"ok": True, "audio": s.audio, "text": s.text}


class SliceDelete(BaseModel):
    audio: str


@router.post("/{human_id}/data-slice-delete")
def delete_slice(human_id: str, body: SliceDelete, source: str = "main", db: Session = Depends(get_db)):
    """删除切片（数据库软删除 + 物理删除音频文件）。"""
    from ..training_slice import TrainingSlice

    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    d = Path(_source_dir(db, h, source))

    audio_name = Path(body.audio).name

    # 1. 数据库软删除
    s = db.execute(
        select(TrainingSlice).where(
            TrainingSlice.human_id == human_id,
            TrainingSlice.audio == audio_name,
            TrainingSlice.is_deleted == 0,
            TrainingSlice.data_dir == _nd(d),
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(400, "未找到该切片")
    s.is_deleted = 1
    db.commit()

    # 2. 物理删除音频文件
    for candidate in [d / audio_name, d / "sliced" / audio_name]:
        if candidate.exists() and candidate.is_relative_to(d):
            candidate.unlink()

    # 3. 重新生成 train.list（从数据库，仅当前目录）
    remaining = db.execute(
        select(TrainingSlice).where(
            TrainingSlice.human_id == human_id,
            TrainingSlice.is_deleted == 0,
            TrainingSlice.data_dir == _nd(h.data_dir),
        ).order_by(TrainingSlice.sort_order)
    ).scalars().all()
    list_lines = [f"{s.audio}|speaker0|zh|{s.text or ''}" for s in remaining]
    (d / "train.list").write_text("\n".join(list_lines), encoding="utf-8")

    return {"ok": True, "audio": audio_name, "remaining": len(remaining)}


@router.get("/{human_id}/data-quality")
def data_quality(human_id: str, db: Session = Depends(get_db)):
    """检查数字人训练数据质量。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    return analyze_training_data(h.data_dir)


class HumanTrainBody(BaseModel):
    s2_epochs: int = 10
    s1_epochs: int = 10
    base_model: str = ""  # 留空=用官方预训练模型；填路径=从上一轮模型继续训练
    s1_source_human_id: str = ""  # s1(语感)数据源数字人 id，留空=当前数字人数据
    s2_source_human_id: str = ""  # s2(音色)数据源数字人 id，留空=当前数字人数据


def _scan_checkpoints(h) -> list[dict]:
    """扫描数字人权重目录，列出所有历史 checkpoint（按轮次），每项含 GPT/SoVITS 权重路径。

    例: model_gpt=F:\\...\\312deb395270_测试数字人-e15.ckpt → 前缀 "312deb395270_测试数字人"，
    扫描同目录 e1~eN 的 .ckpt，并为每个 epoch 配对最新 SoVITS 权重。
    """
    import re
    from pathlib import Path

    gpt_path = h.model_gpt or ""
    if not gpt_path:
        return []
    gpt_file = Path(gpt_path).stem  # 312deb395270_测试数字人-e15
    m = re.match(r"^(.+)-e(\d+)$", gpt_file)
    if not m:
        return []
    exp_prefix = m.group(1)
    gpt_dir = Path(gpt_path).parent
    s2_dir = Path(h.model_s2).parent if h.model_s2 else gpt_dir

    checkpoints = []
    for f in sorted(gpt_dir.glob(f"{exp_prefix}-e*.ckpt")):
        em = re.match(r"^.+-e(\d+)$", f.stem)
        if not em:
            continue
        epoch = int(em.group(1))
        s2_files = sorted(s2_dir.glob(f"{exp_prefix}_e{epoch}_s*.pth"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        s2_path = str(s2_files[0]) if s2_files else ""
        checkpoints.append({
            "epoch": epoch,
            "gpt_path": str(f),
            "s2_path": s2_path,
            "name": f"e{epoch}",
            "is_current": str(f) == gpt_path,
        })
    checkpoints.sort(key=lambda x: x["epoch"])
    return checkpoints


@router.get("/{human_id}/base-models")
def list_base_models(human_id: str, db: Session = Depends(get_db)):
    """可选的基础模型列表：官方预训练 + 所有数字人的历史 checkpoint 版本 + 训练记录（跨数字人）。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")

    # 官方预训练模型
    models = [{"id": "official", "name": "GPT-SoVITS v2 官方预训练", "type": "official"}]

    # 所有数字人的磁盘 checkpoint 版本（每次训练/每轮迭代的完整权重，均可选为基础模型）
    for dh in db.execute(select(DigitalHuman).order_by(DigitalHuman.updated_at.desc())).scalars().all():
        for cp in _scan_checkpoints(dh):
            models.append({
                "id": f"cp:{dh.id}:{cp['epoch']}",
                "name": f"{dh.name} · e{cp['epoch']}" + ("（当前）" if cp["is_current"] else ""),
                "type": "trained",
                "human_id": dh.id,
                "epoch": cp["epoch"],
                "model_s2": cp["s2_path"],
                "model_gpt": cp["gpt_path"],
                "duration": 0,
            })

    # 训练记录（权重未被 checkpoint 扫描覆盖时的补充；跨数字人）
    cp_covered = {(m.get("model_gpt"), m.get("model_s2")) for m in models if m.get("model_gpt")}
    rows = db.execute(
        select(TrainingRecord, DigitalHuman.name).join(
            DigitalHuman, DigitalHuman.id == TrainingRecord.human_id, isouter=True
        ).where(TrainingRecord.status == "success").order_by(TrainingRecord.created_at.desc())
    ).all()
    for r, hname in rows:
        if (r.model_gpt, r.model_s2) in cp_covered:
            continue  # 已由 checkpoint 列表覆盖，避免重复
        name = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "?"
        models.append({
            "id": r.id,
            "name": f"{hname or '未知数字人'} · {name}",
            "type": "trained",
            "human_id": r.human_id,
            "model_s2": r.model_s2,
            "model_gpt": r.model_gpt,
            "duration": r.duration_sec,
        })
    return models


@router.get("/{human_id}/checkpoints")
def list_checkpoints(human_id: str, db: Session = Depends(get_db)):
    """列出该数字人所有可用的 checkpoint（按轮次排序）。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    return _scan_checkpoints(h)


@router.post("/{human_id}/checkpoint")
def set_checkpoint(human_id: str, body: dict, db: Session = Depends(get_db)):
    """切换 checkpoint（更新数字人的 model_gpt/model_s2 和关联 Voice 的 engine_config）。"""
    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    gpt_path = body.get("gpt_path", "")
    s2_path = body.get("s2_path", "")
    if not gpt_path:
        raise HTTPException(400, "gpt_path 必填")
    h.model_gpt = gpt_path
    h.model_s2 = s2_path or h.model_s2
    # 同步更新关联 Voice 的 engine_config
    if h.voice_id:
        from ..models import Voice
        v = db.get(Voice, h.voice_id)
        if v:
            ec = dict(v.engine_config or {})
            ec["gpt_model"] = gpt_path
            ec["sovits_model"] = s2_path or h.model_s2
            ec["finetuned"] = True
            v.engine_config = ec
    db.commit()
    return {"ok": True, "gpt_path": gpt_path, "s2_path": s2_path}


@router.post("/{human_id}/train")
def train_human(human_id: str, body: HumanTrainBody, db: Session = Depends(get_db)):
    """数字人独立训练：从自己的 data_dir + TrainingSlice 数据库生成 train.list → 训练模型。

    不依赖声音库的 train_data 目录，数字人的训练数据、模型完全独立。
    s1(语感)/s2(音色) 数据源：使用数字人自带的 s1_data_dir / s2_data_dir
    （可在「训练数据」标签页分别上传；为空时回退主 data_dir）。
    """
    from ..training_slice import TrainingSlice
    from ..training_record import TrainingRecord
    from ..models import Voice

    def _build_train_list(src_human_id: str, data_dir: Path, label: str) -> int:
        """为任意数据目录生成 train.list 并校验数据（返回总时长秒）。"""
        src_slices = db.execute(
            select(TrainingSlice).where(
                TrainingSlice.human_id == src_human_id,
                TrainingSlice.is_deleted == 0,
                TrainingSlice.text != "",
                or_(TrainingSlice.data_dir == _nd(data_dir), TrainingSlice.data_dir == ""),
            ).order_by(TrainingSlice.sort_order)
        ).scalars().all()
        if not src_slices:
            raise HTTPException(400, f"{label}训练数据为空，请先在「训练数据」标签页上传音频并处理")
        missing = [s.audio for s in src_slices if not (data_dir / s.audio).exists()]
        if missing:
            raise HTTPException(400, f"{label}有 {len(missing)} 个音频文件缺失，可能已被删除，请重新处理")
        (data_dir / "train.list").write_text(
            "\n".join(f"{s.audio}|speaker0|zh|{s.text}" for s in src_slices), encoding="utf-8")
        total = sum(audio_duration(data_dir / s.audio) for s in src_slices)
        if total < 3:
            raise HTTPException(400, f"{label}训练音频合计仅 {total:.0f} 秒（过短无法训练），请上传更长的音频")
        return total, len(src_slices)

    h = db.get(DigitalHuman, human_id)
    if not h:
        raise HTTPException(404, "数字人不存在")
    if not h.voice_id:
        raise HTTPException(400, "请先绑定声音模型（概况标签页）")

    # 数据源目录：s1/s2 各自独立（未真正放音频 = 回退主 data_dir）。
    # 注意判据必须是"目录里有没有音频"而不是"目录存不存在"：_resolve_data_dir
    # 在用户仅点开 s1/s2 标签时就会创建并持久化派生目录，此时目录存在但为空，
    # 若按存在性判断则不会回退，训练会卡在"训练数据为空"。
    def _has_audio(p) -> bool:
        if not p:
            return False
        d = Path(p)
        return d.is_dir() and any(
            f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in ALLOWED_AUDIO_EXT
        )

    main_d = Path(h.data_dir) if _has_audio(h.data_dir) else None
    s1_d = Path(h.s1_data_dir) if _has_audio(h.s1_data_dir) else main_d
    s2_d = Path(h.s2_data_dir) if _has_audio(h.s2_data_dir) else main_d
    if s1_d is None or s2_d is None:
        names = [f"{n}（{lab}）" for n, d, lab in (("s1", s1_d, "语感"), ("s2", s2_d, "音色")) if d is None]
        raise HTTPException(400, f"训练数据不足：{('、'.join(names))} 数据源为空，请上传对应音频或设置主数据")

    # 为每个实际用到的数据源生成 train.list 并校验
    used: dict[str, str] = {}   # 目录 -> 标签（去重：s1==s2==主 时只校验一次）
    for d, label in ((s1_d, "s1 语感数据"), (s2_d, "s2 音色数据")):
        used.setdefault(str(d), label)
    totals: dict[str, int] = {}
    for d_str, label in used.items():
        total, _ = _build_train_list(human_id, Path(d_str), label)
        totals[d_str] = total
    total_sec = totals.get(str(s2_d), 0) or totals.get(str(s1_d), 0)
    main_file_count = totals.get(str(main_d), 0) if main_d else 0

    # 独立源目录传给 trainer（None = 使用主数据）
    s1_dir = s1_d if (main_d is None or s1_d != main_d) else None
    s2_dir = s2_d if (main_d is None or s2_d != main_d) else None
    s1_src_label = "s1 语感数据" if s1_dir else "主数据"
    s2_src_label = "s2 音色数据" if s2_dir else "主数据"

    # 训练中状态（防止重复训练）
    if h.status == "training":
        raise HTTPException(400, "训练正在进行中，请等待完成")

    s2 = max(2, min(100, body.s2_epochs))
    s1 = max(2, min(100, body.s1_epochs))

    # 决定基础模型（支持：TrainingRecord id 或 "cp:{数字人id}:{epoch}" 磁盘 checkpoint 版本）
    use_base = body.base_model and body.base_model != "official"
    base_s2 = base_gpt = ""
    if use_base:
        if body.base_model.startswith("cp:"):
            parts = body.base_model.split(":")
            if len(parts) != 3:
                raise HTTPException(400, "基础模型参数无效")
            _, bhid, bepoch = parts
            bh = db.get(DigitalHuman, bhid)
            cps = _scan_checkpoints(bh) if bh else []
            cp = next((c for c in cps if str(c["epoch"]) == bepoch), None)
            if not cp or not cp["s2_path"]:
                raise HTTPException(400, "所选 checkpoint 不存在或权重缺失")
            base_s2, base_gpt = cp["s2_path"], cp["gpt_path"]
        else:
            base_rec = db.get(TrainingRecord, body.base_model)
            if not base_rec or base_rec.status != "success":
                raise HTTPException(400, "所选基础模型不存在或未完成训练")
            if not base_rec.model_s2 or not base_rec.model_gpt:
                raise HTTPException(400, "所选基础模型的权重文件缺失")
            base_s2, base_gpt = base_rec.model_s2, base_rec.model_gpt

    # 标记为训练中
    h.status = "training"
    db.commit()

    def _run(update):
        import time as _time
        _t0 = _time.time()
        from ..db import SessionLocal
        from ..gpt_sovits_train import GPTSoVITSTrainer, GPT_SOVITS_ROOT, PRETRAINED_S2G, PRETRAINED_S2D

        d2 = SessionLocal()
        try:
            h2 = d2.get(DigitalHuman, human_id)
            if h2 is None:
                raise RuntimeError("数字人已被删除")

            trainer = GPTSoVITSTrainer(f"{h2.id}_{h2.name}", update)

            # 如果选了基础模型：s2（音色）和 s1（语义）都从基础模型继续迭代训练
            if use_base:
                trainer.override_pretrained(
                    pretrained_s2G=base_s2,
                    pretrained_s2D=base_s2,
                    pretrained_s1=base_gpt,
                )

            result = trainer.run(main_d, h2.name or "speaker0", s2, s1,
                                 s1_input_dir=s1_dir, s2_input_dir=s2_dir)

            # 记录模型路径
            h2.model_s2 = result.get("sovits_model", "")
            h2.model_gpt = result.get("gpt_model", "")
            h2.status = "trained"
            h2.training_config = {
                "sovits": {"batch_size": 4, "epochs": s2, "fp16": True, "source": s2_src_label},
                "gpt": {"batch_size": 4, "epochs": s1, "dpo": False, "source": s1_src_label},
            }

            # 每次迭代训练生成一个版本记录（可作为后续训练的基础模型）
            from ..training_record import TrainingRecord as TR
            d2.add(TR(
                human_id=h2.id,
                voice_id=h2.voice_id or "",
                data_dir=_nd(s2_d or main_d or s1_d),
                data_file_count=main_file_count,
                data_duration=total_sec,
                pretrained_s2=base_s2,
                pretrained_s1=base_gpt,
                model_s2=h2.model_s2,
                model_gpt=h2.model_gpt,
                params={
                    "s2_epochs": s2, "s1_epochs": s1, "base_model": body.base_model,
                    "s1_source": s1_src_label, "s2_source": s2_src_label,
                },
                status="success",
                message="迭代训练完成",
                duration_sec=max(1, int(_time.time() - _t0)),
            ))

            # 同步模型到声音库（数字人训练的模型也绑定到声音，供合成使用）
            if h2.voice_id:
                v2 = d2.get(Voice, h2.voice_id)
                if v2:
                    v2.engine_config = dict(v2.engine_config or {})
                    v2.engine_config["gpt_model"] = h2.model_gpt
                    v2.engine_config["sovits_model"] = h2.model_s2
                    v2.engine_config["finetuned"] = True
                    # 设置参考音频（取训练数据中第一段有文字的音频作为 ref）
                    if not v2.engine_config.get("ref_path"):
                        ref_slice = db.execute(
                            select(TrainingSlice).where(
                                TrainingSlice.human_id == human_id,
                                TrainingSlice.is_deleted == 0,
                                TrainingSlice.text != "",
                                or_(TrainingSlice.data_dir == h.data_dir, TrainingSlice.data_dir == ""),
                            ).order_by(TrainingSlice.sort_order).limit(1)
                        ).scalar_one_or_none()
                        if ref_slice:
                            v2.engine_config["ref_path"] = str(d / ref_slice.audio)
                            v2.engine_config["ref_text"] = ref_slice.text
                            v2.engine_config["ref_lang"] = "zh"

            d2.commit()
            update(100, "数字人训练完成！模型已保存")
            return result
        except Exception:
            # 训练失败：恢复数字人状态，允许重新发起训练（job 层会标记 error）
            try:
                h2 = d2.get(DigitalHuman, human_id)
                if h2 is not None and h2.status == "training":
                    h2.status = "untrained"
                    d2.commit()
            except Exception:  # noqa: BLE001
                d2.rollback()
            raise
        finally:
            d2.close()

    from ..jobs import enqueue
    job_id = enqueue("human_train", f"数字人训练「{h.name}」", _run, gpu=True)
    return {"job_id": job_id}
