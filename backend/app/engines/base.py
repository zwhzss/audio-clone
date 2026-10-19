"""引擎基类与注册表。"""
from __future__ import annotations

from typing import Callable

from ..models import Voice

ProgressCb = Callable[[int, str], None]


class BaseEngine:
    id = "base"
    name = "未命名引擎"
    desc = ""
    supports_clone = False        # 是否支持声音克隆
    supports_vc = False           # 是否支持音色转换（初版音频换声）
    is_cloud = False              # 是否走云端 API（并行/计费）
    needs_key = False
    cost_hint = ""                # 计费提示

    def check(self, db) -> dict:
        """可用性检查：{ok, message, detail}"""
        return {"ok": True, "message": "可用", "detail": ""}

    def list_voices(self, db) -> list[dict]:
        """引擎提供的预设音色（edge 等）。返回 [{id, name, lang}]"""
        return []

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        """用指定声音合成音频，返回 {path, cost, chars, duration}"""
        raise NotImplementedError

    def voice_convert(self, voice: Voice, input_audio: str, out_path: str, update: ProgressCb | None = None) -> dict:
        """音色转换：把初版音频换成本声音的音色，返回 {path, cost}"""
        raise NotImplementedError

    def clone(self, voice: Voice, db, update: ProgressCb | None = None) -> dict:
        """克隆/训练声音模型，返回 {model_id, engine_config}"""
        raise NotImplementedError


_REGISTRY: dict[str, BaseEngine] = {}


def register(engine: BaseEngine):
    _REGISTRY[engine.id] = engine


def get_engine(engine_id: str) -> BaseEngine:
    if engine_id not in _REGISTRY:
        raise ValueError(f"未知引擎: {engine_id}")
    return _REGISTRY[engine_id]


def all_engines() -> list[BaseEngine]:
    return list(_REGISTRY.values())
