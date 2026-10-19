"""Fish Audio 云引擎：支持一次性复刻与平台训练音色（voice id）。

文档: https://fishaudio.org/zh/api-reference
计费：平台按积分计费（可在设置中调整单价，默认按 ¥5/万字估算）。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from ..audio_utils import audio_duration, convert_to_wav
from ..models import Voice, get_active_model, get_setting
from .base import BaseEngine, ProgressCb

API_BASE = "https://api.fish.audio"


class FishEngine(BaseEngine):
    id = "fish"
    name = "Fish Audio（云 API）"
    desc = "云端克隆 TTS：参考音频一次性复刻，或训练专属音色；中文效果好，API 简洁"
    supports_clone = True
    supports_vc = False
    is_cloud = True
    needs_key = True
    cost_hint = "积分制，约 ¥5/万字（可调）"

    def _model(self, db) -> dict | None:
        m = get_active_model(db, "voice")
        if m is not None and m.provider == "fish":
            return {"api_key": m.api_key, "price": m.price or 5.0}
        key = get_setting(db, "fish_api_key", "")
        if key:
            return {"api_key": key, "price": float(get_setting(db, "fish_price_per_10k", "5") or 5)}
        return None

    def check(self, db) -> dict:
        m = self._model(db)
        active = get_active_model(db, "voice")
        if m is None:
            if active is not None and active.provider != "fish":
                return {
                    "ok": False,
                    "message": "未激活 Fish（当前语音模型：%s）" % active.name,
                    "detail": "在「模型库」中激活 Fish Audio 即可切换",
                }
            return {"ok": False, "message": "未配置密钥", "detail": "在「模型库」中追加/激活 Fish Audio 并填写 API Key"}
        if not m["api_key"]:
            return {"ok": False, "message": "密钥未填写", "detail": "请在模型库中补充 Fish Audio API Key"}
        return {"ok": True, "message": "已激活", "detail": "积分制计费"}

    def clone(self, voice: Voice, db, update: ProgressCb | None = None) -> dict:
        """尝试在平台训练专属音色（POST /v1/voices）；失败则回退为一次性复刻。"""
        if update:
            update(20, "校验参考音频…")
        if not voice.ref_audio or not Path(voice.ref_audio).exists():
            raise RuntimeError("缺少参考音频")
        dur = audio_duration(voice.ref_audio)
        if dur < 3:
            raise RuntimeError("参考音频过短（<3 秒），建议 10~60 秒清晰人声")

        m = self._model(db)
        if m is None or not m["api_key"]:
            raise RuntimeError("未激活 Fish Audio（模型库）")
        key = m["api_key"]
        cfg = dict(voice.engine_config or {})
        name = voice.name or f"voice_{voice.id}"
        if update:
            update(50, "提交平台音色训练…（通常需几分钟）")
        try:
            with open(voice.ref_audio, "rb") as f:
                resp = httpx.post(
                    f"{API_BASE}/v1/voices",
                    headers={"Authorization": f"Bearer {key}"},
                    data={"title": name, "reference_text": voice.ref_text or ""},
                    files={"files": ("ref.wav", f, "audio/wav")},
                    timeout=120,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                vid = data.get("id") or data.get("voice_id") or ""
                if vid:
                    cfg["trained"] = True
                    if update:
                        update(100, "训练已提交，音色就绪")
                    return {"model_id": vid, "engine_config": cfg}
            raise RuntimeError(f"训练接口失败 (HTTP {resp.status_code}): {resp.text[:300]}")
        except Exception as e:  # noqa: BLE001
            if update:
                update(70, "训练不可用，回退一次性复刻模式…")
            cfg["one_shot_clone"] = True
            if update:
                update(100, "克隆就绪（一次性复刻模式）")
            return {"model_id": "one-shot", "engine_config": cfg}

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            m = self._model(db)
            if m is None:
                raise RuntimeError("未激活 Fish Audio 语音模型（模型库）")
            if not m["api_key"]:
                raise RuntimeError("Fish Audio API Key 未填写（模型库）")
            key, price = m["api_key"], m["price"]
        finally:
            db.close()

        cfg = voice.engine_config or {}
        body: dict = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "normal",
            "normalize": True,
        }
        if voice.model_id and voice.model_id != "one-shot":
            body["reference_id"] = voice.model_id
        elif voice.ref_audio and Path(voice.ref_audio).exists():
            b64 = base64.b64encode(Path(voice.ref_audio).read_bytes()).decode()
            body["reference_audio"] = b64
            if voice.ref_text:
                body["reference_text"] = voice.ref_text
        else:
            raise RuntimeError("该声音缺少参考音频或模型 id，请先克隆")

        if update:
            update(20, "请求 Fish Audio 合成…")
        resp = httpx.post(
            f"{API_BASE}/v1/tts",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=300,
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
                raise RuntimeError(f"Fish Audio 错误: {err.get('message', resp.text[:300])}")
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001
                raise RuntimeError(f"Fish Audio 错误 (HTTP {resp.status_code}): {resp.text[:300]}")

        Path(out_path).write_bytes(resp.content)
        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(out_path, wav)
        chars = len(text)
        cost = round(chars / 10000 * price, 4)
        if update:
            update(100, f"完成（{chars} 字符，约 ¥{cost:.4f}）")
        return {"path": wav, "cost": cost, "chars": chars, "duration": audio_duration(wav)}


_engine = FishEngine()
