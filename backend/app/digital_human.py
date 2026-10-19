"""数字人模块：绑定声音模型 + 润色设置 + 训练配置的完整身份。

数字人 = 一个专属的声音身份（基准音色 + 润色配置 + 微调模型 + 训练数据），用它对任意文学作品进行朗读/配音。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, select

from .db import Base
from .config import SAMPLE_RATE
from .audio_polish import build_polish_filter, render_polish
from .models import _now, _uid, get_setting


class DigitalHuman(Base):
    """数字人：专属声音身份。"""

    __tablename__ = "digital_humans"

    id = Column(String(16), primary_key=True, default=_uid)
    name = Column(String(100), default="")                        # 数字人名称
    desc = Column(Text, default="")                               # 描述
    voice_id = Column(String(16), default=None)                   # 关联声音模型 ID
    polish_params = Column(JSON, default=dict)                    # 润色参数（EQ/混响/压缩等）
    training_config = Column(JSON, default=dict)                  # 训练配置（epochs/batch/temp 等）
    data_dir = Column(String(500), default="")                    # 训练数据目录（主数据/默认源）
    s1_data_dir = Column(String(500), default="")                 # s1 语感训练数据目录（空=用 data_dir）
    s2_data_dir = Column(String(500), default="")                 # s2 音色训练数据目录（空=用 data_dir）
    data_stats = Column(JSON, default=dict)                       # 数据统计（时长/句子数/文体占比）
    model_s2 = Column(String(500), default="")                    # SoVITS 权重路径
    model_gpt = Column(String(500), default="")                   # GPT 权重路径
    inference_params = Column(JSON, default=dict)                 # 推理参数（temp/top_p/top_k）
    status = Column(String(20), default="untrained")              # untrained/training/trained/active
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
        return d


# ---------- 8GB GPU 专属训练参数（千问方案优化） ----------

TRAINING_PRESETS_8GB = {
    "sovits": {
        "batch_size": 4,
        "learning_rate": 0.0001,
        "epochs": 15,
        "save_every_epoch": 5,
        "grad_checkpoint": True,
        "fp16_run": True,
        "gradient_accumulation": 4,
        "desc": "SoVITS 训练（音色提取）：batch=4 模拟 batch=8，10-15轮",
    },
    "gpt": {
        "batch_size": 4,
        "learning_rate": 0.0001,
        "epochs": 15,
        "save_every_epoch": 3,
        "fp16": True,
        "gradient_accumulation": 4,
        "warmup_steps": 200,
        "dpo": False,                    # 8GB 显存必须关闭 DPO
        "desc": "GPT 训练（韵律/语感）：batch=4，10-15轮，禁止DPO",
    },
}

# ---------- 推理参数模板（三家方案综合） ----------

INFERENCE_PRESETS = {
    "文学朗读": {
        "temperature": 0.72,
        "top_p": 0.85,
        "top_k": 15,
        "repetition_penalty": 1.05,
        "desc": "文学作品推荐：情感丰富又稳定（豆包+千问推荐值）",
    },
    "稳定朗读": {
        "temperature": 0.6,
        "top_p": 0.8,
        "top_k": 10,
        "repetition_penalty": 1.05,
        "desc": "正式场合/新闻：发音准确稳定",
    },
    "生动演绎": {
        "temperature": 0.85,
        "top_p": 0.9,
        "top_k": 20,
        "repetition_penalty": 1.0,
        "desc": "小说/故事：情感更丰富（注意可能偶尔不稳定）",
    },
}


# ---------- 数据质量检查 ----------

def analyze_training_data(data_dir: str) -> dict:
    """分析训练数据目录，返回质量报告。"""
    from pathlib import Path

    d = Path(data_dir)
    if not d.exists():
        return {"error": "目录不存在"}

    wav_files = list(d.rglob("*.wav"))
    total_sec = 0.0
    content_types = {"prose": 0, "poetry": 0, "fiction": 0, "classical": 0, "other": 0}

    # 读取 list 文件统计内容类型
    list_files = list(d.rglob("*.list"))
    if not list_files:
        # 扫描原始音频
        for wav in wav_files:
            total_sec += _wav_duration(wav)
    else:
        for lf in list_files:
            for line in lf.read_text(encoding="utf-8").splitlines():
                parts = line.split("|")
                if len(parts) >= 4:
                    text = parts[3]
                    ct = _classify_text(text)
                    content_types[ct] += 1
                # 估算时长（每段约 5 秒）
                total_sec += 5.0

    total_min = total_sec / 60
    total_files = len(wav_files) or len(list_files) or len(list(d.iterdir()))
    sentences = sum(content_types.values())

    # 质量评分
    score = 0
    issues = []
    if total_min < 60:
        score -= 30
        issues.append(f"数据量不足：仅 {total_min:.1f} 分钟，建议至少 1-2 小时")
    elif total_min < 180:
        score += 40
        issues.append(f"数据量可接受（{total_min:.1f} 分钟），增加到 3 小时效果更好")
    else:
        score += 80
        issues.append(f"数据量充足（{total_min:.1f} 分钟）")

    # 文体多样性
    nonzero = sum(1 for v in content_types.values() if v > 0)
    if nonzero >= 3:
        score += 20
        issues.append("文体多样（≥3 类）✓")
    elif nonzero == 2:
        score += 10
        issues.append("文体偏少，建议加入更多文体（诗歌/散文/小说）")
    else:
        issues.append("文体单一，强烈建议加入多种文体")

    return {
        "total_minutes": round(total_min, 1),
        "total_files": total_files,
        "sentences": sentences,
        "content_types": content_types,
        "quality_score": max(0, min(100, score)),
        "issues": issues,
        "recommendation": "推荐：散文 30% + 诗歌 20% + 小说 30% + 古文 20%" if nonzero < 3 else "文体配比合理",
    }


def _classify_text(text: str) -> str:
    """粗略分类文本内容类型。"""
    import re

    text = text.strip()
    # 古文：含古文特征词或句式
    if re.search(r"[矣兮兮乎也者]$", text) or re.search(r"之乎者也|子曰|诗曰", text):
        return "classical"
    # 小说：含对话引号（中文书名号也算对话标记）
    if text.count(""") + text.count(""") + text.count('"') + text.count("「") + text.count("」") > 2:
        return "fiction"
    # 诗歌：短句+特定标点模式（逗号结尾+短句，或句号结尾但极短）
    if re.search(r"[，]$", text) and len(text) < 40:
        return "poetry"
    return "prose"


def _wav_duration(path) -> float:
    """估算 wav 时长（秒）。"""
    try:
        size = Path(path).stat().st_size
        return size / (SAMPLE_RATE * 2 * 1)  # 16bit mono 44.1k ~ 每秒约 88KB
    except Exception:  # noqa: BLE001
        return 5.0
