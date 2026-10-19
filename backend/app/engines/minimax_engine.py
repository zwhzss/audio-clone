"""MiniMax 云引擎：speech-02 系列 TTS，支持一次性声音复刻（参考音频随请求上传）。

计费：speech-02 约 ¥3/万字，speech-02-hd 约 ¥4/万字（可在设置中调整单价）。
文档: https://platform.minimaxi.com/docs/guides/pricing-speech
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..audio_utils import audio_duration, convert_to_wav
from ..models import Voice, get_active_model, get_setting
from .base import BaseEngine, ProgressCb

API_BASE = "https://api.minimaxi.com"

PRESET_VOICES = [
    {"id": "male-qn-qingse", "name": "男·青涩", "lang": "zh"},
    {"id": "male-shengdai", "name": "男·声带（沉稳）", "lang": "zh"},
    {"id": "male-chengshu", "name": "男·成熟", "lang": "zh"},
    {"id": "female-shaonv", "name": "女·少女", "lang": "zh"},
    {"id": "female-yujie", "name": "女·御姐", "lang": "zh"},
    {"id": "female-chengshu", "name": "女·成熟", "lang": "zh"},
    {"id": "female-tianmei", "name": "女·甜美", "lang": "zh"},
    {"id": "audiobook_male_1", "name": "有声书·男1", "lang": "zh"},
    {"id": "audiobook_male_2", "name": "有声书·男2", "lang": "zh"},
    {"id": "audiobook_female_1", "name": "有声书·女1", "lang": "zh"},
    {"id": "audiobook_female_2", "name": "有声书·女2", "lang": "zh"},
    {"id": "presenter_male", "name": "播音·男", "lang": "zh"},
    {"id": "presenter_female", "name": "播音·女", "lang": "zh"},
]


class MiniMaxEngine(BaseEngine):
    id = "minimax"
    name = "MiniMax（云 API）"
    desc = "speech-02 系列云端 TTS：一次请求用参考音频复刻音色，或使用预设音色；音质高、性价比好"
    supports_clone = True
    supports_vc = False
    is_cloud = True
    needs_key = True
    cost_hint = "约 ¥3~4 / 万字（模型库中可调）"

    def _model(self, db) -> dict | None:
        """从模型库读取激活的 MiniMax 语音模型（含 settings 兼容回退）。"""
        m = get_active_model(db, "voice")
        if m is not None and m.provider == "minimax":
            return {
                "api_key": m.api_key,
                "group_id": (m.extra or {}).get("group_id", ""),
                "model": m.model or "speech-02-hd",
                "price": m.price or 4.0,
            }
        # 兼容回退：旧 settings
        key = get_setting(db, "minimax_api_key", "")
        if key:
            return {
                "api_key": key,
                "group_id": get_setting(db, "minimax_group_id", ""),
                "model": get_setting(db, "minimax_model", "speech-02-hd"),
                "price": float(get_setting(db, "minimax_price_per_10k", "4") or 4),
            }
        return None

    def check(self, db) -> dict:
        m = self._model(db)
        active = get_active_model(db, "voice")
        if m is None:
            if active is not None and active.provider != "minimax":
                return {
                    "ok": False,
                    "message": "未激活 MiniMax（当前语音模型：%s）" % active.name,
                    "detail": "在「模型库」中激活 MiniMax 即可切换",
                }
            return {"ok": False, "message": "未配置密钥", "detail": "在「模型库」中追加/激活 MiniMax 并填写 API Key"}
        if not m["api_key"] or not m["group_id"]:
            return {"ok": False, "message": "密钥不完整", "detail": "请在模型库中补充 MiniMax API Key 与 GroupId"}
        return {"ok": True, "message": "已激活（%s）" % m["model"], "detail": "约 ¥%s/万字" % m["price"]}

    def list_voices(self, db) -> list[dict]:
        return PRESET_VOICES

    def clone(self, voice: Voice, db, update: ProgressCb | None = None) -> dict:
        if update:
            update(30, "校验参考音频…")
        if not voice.ref_audio or not Path(voice.ref_audio).exists():
            raise RuntimeError("缺少参考音频")
        dur = audio_duration(voice.ref_audio)
        if dur < 3:
            raise RuntimeError("参考音频过短（<3 秒），建议 10~60 秒清晰人声")
        if update:
            update(80, "MiniMax 采用请求时复刻：合成时会自动携带参考音频…")
        cfg = dict(voice.engine_config or {})
        cfg["one_shot_clone"] = True
        if update:
            update(100, "克隆就绪（云复刻模式）")
        return {"model_id": voice.model_id or "one-shot", "engine_config": cfg}

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            m = self._model(db)
            if m is None:
                raise RuntimeError("未激活 MiniMax 语音模型（模型库）")
            if not m["api_key"]:
                raise RuntimeError("MiniMax API Key 未填写（模型库）")
            if not m["group_id"]:
                raise RuntimeError("MiniMax GroupId 未填写（模型库）")
            key, group, model, price = m["api_key"], m["group_id"], m["model"], m["price"]
        finally:
            db.close()

        cfg = voice.engine_config or {}
        voice_setting = {
            "voice_id": voice.model_id or "",
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
            "audio_sample_rate": 32000,
            "bitrate": 128000,
        }
        if cfg.get("speed"):
            voice_setting["speed"] = float(cfg["speed"])

        if update:
            update(20, "请求 MiniMax 语音合成…")
        form = {
            "model": (None, model),
            "text": (None, text),
            "voice_setting": (None, json.dumps(voice_setting, ensure_ascii=False)),
        }
        # 一次性复刻：携带参考音频
        if not voice.model_id and voice.ref_audio and Path(voice.ref_audio).exists():
            form["audio"] = ("ref.wav", open(voice.ref_audio, "rb"), "audio/wav")
            if voice.ref_text:
                form["audio_text"] = (None, voice.ref_text)
        elif not voice.model_id and not cfg.get("one_shot_clone"):
            raise RuntimeError("该声音无 model_id，请先克隆或选择预设音色")

        try:
            resp = httpx.post(
                f"{API_BASE}/v1/t2a_v2?GroupId={group}",
                headers={"Authorization": f"Bearer {key}"},
                files=form,
                timeout=300,
            )
        finally:
            if "audio" in form and hasattr(form["audio"][1], "close"):
                form["audio"][1].close()

        if resp.status_code != 200:
            try:
                err = resp.json()
                raise RuntimeError(f"MiniMax 错误: {err.get('base_resp', {}).get('status_msg', resp.text[:300])}")
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001
                raise RuntimeError(f"MiniMax 错误 (HTTP {resp.status_code}): {resp.text[:300]}")

        Path(out_path).write_bytes(resp.content)
        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(out_path, wav)
        chars = len(text)
        cost = round(chars / 10000 * price, 4)
        if update:
            update(100, f"完成（{chars} 字符，约 ¥{cost:.4f}）")
        return {"path": wav, "cost": cost, "chars": chars, "duration": audio_duration(wav)}


_engine = MiniMaxEngine()
