"""数据模型：声音模型 / 文稿 / 项目 / 分段 / 任务 / 设置。"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, select

from .db import Base


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now()


class Voice(Base):
    """声音模型（声音数字人）。source: edge=固定音色 / gpt_sovits=本地克隆 / minimax=云克隆 / fish=云克隆"""

    __tablename__ = "voices"

    id = Column(String(16), primary_key=True, default=_uid)
    name = Column(String(100), nullable=False)          # 展示名，如「我的声音」
    source = Column(String(30), default="edge")          # 引擎来源
    ref_audio = Column(String(500), default="")          # 参考音频路径
    ref_text = Column(Text, default="")                  # 参考音频对应文本（克隆用）
    ref_lang = Column(String(20), default="zh")          # 参考音频语言
    edge_voice = Column(String(60), default="")          # edge 引擎的固定音色名
    model_id = Column(String(200), default="")           # 云平台/本地模型 id
    status = Column(String(20), default="draft")         # draft/ready/training/error
    error = Column(Text, default="")
    duration = Column(Float, default=0.0)                # 参考音频时长（秒）
    timbre = Column(String(200), default="")             # 音色描述（用户填写）
    engine_config = Column(JSON, default=dict)           # 引擎专属参数（如 sovits 角色 id）
    created_at = Column(DateTime, default=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        return d


class Document(Base):
    """文稿：原文 + LLM 润色结果 + 费用"""

    __tablename__ = "documents"

    id = Column(String(16), primary_key=True, default=_uid)
    title = Column(String(200), default="未命名文稿")
    content = Column(Text, default="")
    polished = Column(Text, default="")          # 润色后文本
    polish_style = Column(String(100), default="")  # 润色风格
    llm_cost = Column(Float, default=0.0)        # 累计润色费用（元）
    llm_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for k in ("created_at", "updated_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        return d


class Project(Base):
    """有声读物项目：文稿 + 声音 + 混音参数 → 成品"""

    __tablename__ = "projects"

    id = Column(String(16), primary_key=True, default=_uid)
    title = Column(String(200), default="未命名作品")
    document_id = Column(String(16), ForeignKey("documents.id"), nullable=True)
    voice_id = Column(String(16), ForeignKey("voices.id"), nullable=True)
    digital_human_id = Column(String(16), nullable=True, default="")  # 关联的数字人ID
    ref_audio_path = Column(String(500), default="")      # 推理参考音频路径
    ref_text = Column(Text, default="")                    # 推理参考文本
    bgm_path = Column(String(500), default="")            # 背景音乐
    params = Column(JSON, default=dict)                   # 混音参数（音量/语速/音调/EQ/淡入淡出/背景乐音量）
    status = Column(String(20), default="draft")          # draft/generating/voices_ready/rendering/ready/error
    final_path = Column(String(500), default="")
    voice_track_path = Column(String(500), default="")    # 拼接后的声音轨（预览用）
    chapters = Column(JSON, default=list)                 # 章节：[{title, start_seg, end_seg}]
    duration = Column(Float, default=0.0)
    error = Column(Text, default="")
    cost = Column(Float, default=0.0)                     # 本项目累计云费用
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for k in ("created_at", "updated_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        return d


class Segment(Base):
    """项目分段：文本 → 初版音频 → 混音后音频"""

    __tablename__ = "segments"

    id = Column(String(16), primary_key=True, default=_uid)
    project_id = Column(String(16), ForeignKey("projects.id"), index=True)
    idx = Column(Integer, default=0)
    text = Column(Text, default="")
    draft_path = Column(String(500), default="")   # 初版音频
    mixed_path = Column(String(500), default="")   # 混音后音频（克隆音色）
    params = Column(JSON, default=dict)            # 分段级混音参数（speed/pitch/volume/eq）
    offset = Column(Float, default=0.0)            # 在声音轨中的起始时间（秒）
    status = Column(String(20), default="pending")  # pending/drafting/mixing/ready/error
    error = Column(Text, default="")
    created_at = Column(DateTime, default=_now)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        return d


class Job(Base):
    """后台任务（TTS/训练/混音/润色）"""

    __tablename__ = "jobs"

    id = Column(String(16), primary_key=True, default=_uid)
    kind = Column(String(30), default="")      # voice_clone/tts/mix/polish/...
    title = Column(String(200), default="")
    status = Column(String(20), default="queued")  # queued/running/success/error
    progress = Column(Integer, default=0)      # 0-100
    message = Column(Text, default="")
    result = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for k in ("created_at", "finished_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        return d


class Setting(Base):
    """键值设置：API 密钥、默认引擎、云价格表等（旧版配置，已被 ai_models 取代，保留兼容）"""

    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class AIModel(Base):
    """商用模型配置（模型库）。kind: voice=语音合成/克隆, llm=文稿润色"""

    __tablename__ = "ai_models"

    id = Column(String(16), primary_key=True, default=_uid)
    kind = Column(String(20), default="voice")        # voice / llm
    provider = Column(String(30), default="")         # minimax / fish / deepseek / qwen / glm / openvino / edge / custom ...
    name = Column(String(100), default="")            # 展示名
    model = Column(String(100), default="")           # 模型名（speech-02-hd / deepseek-chat）
    api_key = Column(String(500), default="")
    base_url = Column(String(500), default="")
    price = Column(Float, default=0.0)                # 单价（元）
    price_output = Column(Float, default=0.0)         # LLM 输出单价（元/百万token）
    price_unit = Column(String(30), default="万字符")  # 万字符 / 百万token / 免费
    desc = Column(Text, default="")
    extra = Column(JSON, default=dict)                # 平台专属（minimax group_id、openvino model_dir/device）
    is_active = Column(Integer, default=0)            # 当前激活（同 kind 仅一个）
    is_preset = Column(Integer, default=0)            # 是否缺省预置
    created_at = Column(DateTime, default=_now)

    def to_dict(self, mask_key: bool = True):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        if mask_key and d.get("api_key"):
            k = d["api_key"]
            d["api_key"] = (k[:4] + "****" + k[-4:]) if len(k) > 8 else "*" * len(k)
        return d


def get_active_model(db, kind: str) -> AIModel | None:
    """当前激活的模型（voice 类可回退到 edge 免费；llm 类无激活时返回 None）。"""
    m = db.execute(
        select(AIModel).where(AIModel.kind == kind, AIModel.is_active == 1)
    ).scalars().first()
    if m is not None:
        return m
    # 兼容：voice 类无激活时默认 edge 免费音色
    if kind == "voice":
        return db.execute(
            select(AIModel).where(AIModel.kind == "voice", AIModel.provider == "edge")
        ).scalars().first()
    return None


# ---------- 便捷读写 ----------

def get_setting(db, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def get_setting_json(db, key: str, default):
    raw = get_setting(db, key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_setting_json(db, key: str, value):
    set_setting(db, key, json.dumps(value, ensure_ascii=False))
