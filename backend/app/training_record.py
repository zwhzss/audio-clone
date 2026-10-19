"""训练记录与数据去重：每次训练的历史追踪 + SHA256 哈希比对防止数据重复。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

from .db import Base
from .models import _uid, _now


class TrainingRecord(Base):
    """训练记录：每次训练的完整信息。"""

    __tablename__ = "training_records"

    id = Column(String(16), primary_key=True, default=_uid)
    human_id = Column(String(16), index=True)                     # 所属数字人
    voice_id = Column(String(16), default="")                     # 训练用声音
    data_dir = Column(String(500), default="")                    # 训练数据目录
    data_hash = Column(JSON, default=list)                        # 训练数据文件 SHA256 列表
    data_file_count = Column(Integer, default=0)                  # 文件数
    data_duration = Column(Float, default=0.0)                    # 总时长（秒）
    pretrained_s2 = Column(String(500), default="")               # 旧 s2 模型（起点）
    pretrained_s1 = Column(String(500), default="")               # 旧 s1 模型（起点）
    model_s2 = Column(String(500), default="")                    # 新 s2 产出
    model_gpt = Column(String(500), default="")                   # 新 s1 产出
    params = Column(JSON, default=dict)                           # 训练参数
    status = Column(String(20), default="running")                # running/success/error
    message = Column(Text, default="")                            # 完成信息
    duration_sec = Column(Integer, default=0)                     # 训练耗时（秒）
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        d["finished_at"] = d["finished_at"].isoformat() if d.get("finished_at") else None
        return d


def scan_data_hashes(data_dir: str) -> list[dict]:
    """扫描数据目录，返回每个 .wav 文件的路径+SHA256。"""
    if not data_dir or not Path(data_dir).exists():
        return []
    results = []
    for p in Path(data_dir).rglob("*.wav"):
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            results.append({
                "path": str(p),
                "size": p.stat().st_size,
                "sha256": h,
            })
        except Exception:  # noqa: BLE001
            pass
    return results


def check_duplicates(new_hashes: list[dict], history_hashes: list[dict]) -> dict:
    """比对新数据与历史数据的重复情况。

    history_hashes: 历史训练记录中所有使用过的文件哈希集合（去重）。
    返回重复文件列表与比例。
    """
    if not new_hashes or not history_hashes:
        return {"overlapping": 0, "total": len(new_hashes), "ratio": 0.0, "files": []}
    history_set = {h["sha256"] for h in history_hashes}
    overlap = [h for h in new_hashes if h["sha256"] in history_set]
    total = len(new_hashes)
    ratio = len(overlap) / total if total > 0 else 0.0
    return {
        "overlapping": len(overlap),
        "total": total,
        "ratio": round(ratio, 3),
        "files": [f for f, _ in [(h["path"], h["sha256"]) for h in overlap]],
    }
