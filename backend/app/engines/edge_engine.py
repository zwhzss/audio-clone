"""Edge TTS 引擎：微软免费神经网络音色，无需密钥，用于初版音频/基础合成。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..audio_utils import convert_to_wav
from ..models import Voice
from .base import BaseEngine, ProgressCb

# 常用中文/英文音色（更多见 https://github.com/rany2/edge-tts）
# 仅保留实测可用的音色：微软已陆续下线部分 zh-CN 神经音色，请求会返回
# "No audio was received"，故移除 云枫/晓辰/晓涵/晓梦/晓秋/晓睿/晓双/
# 晓颜/晓悠/云皓/云泽 共 11 个。新增前请先用 edge-tts 实际合成验证。
PRESET_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓（女·温柔）", "lang": "zh"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊（女·活力）", "lang": "zh"},
    {"id": "zh-CN-YunxiNeural", "name": "云希（男·阳光）", "lang": "zh"},
    {"id": "zh-CN-YunjianNeural", "name": "云健（男·浑厚）", "lang": "zh"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬（男·新闻）", "lang": "zh"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏（男·少年）", "lang": "zh"},
    {"id": "zh-CN-XiaoxuanNeural", "name": "晓萱（女·清亮）", "lang": "zh"},
    {"id": "zh-CN-XiaobeiNeural", "name": "晓北（女·方言）", "lang": "zh"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "曉臻（女·台湾）", "lang": "zh-tw"},
    {"id": "zh-HK-HiuMaanNeural", "name": "曉曼（女·粤语）", "lang": "zh-hk"},
    {"id": "en-US-AriaNeural", "name": "Aria（女·美音）", "lang": "en"},
    {"id": "en-US-GuyNeural", "name": "Guy（男·美音）", "lang": "en"},
    {"id": "en-GB-SoniaNeural", "name": "Sonia（女·英音）", "lang": "en"},
    {"id": "ja-JP-NanamiNeural", "name": "七海（女·日语）", "lang": "ja"},
]


class EdgeEngine(BaseEngine):
    id = "edge"
    name = "Edge 免费音色"
    desc = "微软神经网络语音，完全免费、无需密钥、联网即可用；适合生成初版音频与日常合成"
    supports_clone = False
    supports_vc = False
    is_cloud = False
    cost_hint = "免费"

    def check(self, db) -> dict:
        return {"ok": True, "message": "联网即可使用", "detail": "无需配置密钥"}

    def list_voices(self, db) -> list[dict]:
        return PRESET_VOICES

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        import edge_tts

        if not voice.edge_voice:
            voice.edge_voice = "zh-CN-XiaoxiaoNeural"

        rate = "+0%"
        pitch = "+0Hz"
        if voice.engine_config:
            rate = voice.engine_config.get("rate", "+0%")
            pitch = voice.engine_config.get("pitch", "+0Hz")

        async def _run(seen: list[str] | None = None):
            comm = edge_tts.Communicate(text, voice.edge_voice, rate=rate, pitch=pitch)
            try:
                async for chunk in comm.stream():
                    if seen is not None:
                        seen.append(chunk.get("type", "?"))
                    if chunk.get("type") == "audio":
                        with open(out_path, "ab") as f:
                            f.write(chunk["data"])
            except Exception as e:  # noqa: BLE001
                extra = f" [ws:{','.join(seen)}|state:{comm.state}]" if seen is not None else ""
                raise RuntimeError(f"{type(e).__name__}: {e}{extra}")

        if update:
            update(30, "正在请求 Edge 语音服务…")
        Path(out_path).unlink(missing_ok=True)
        seen: list[str] = []
        try:
            asyncio.run(_run(seen))
        except Exception as e:  # noqa: BLE001
            # Edge 服务偶发抽风（限流/断流）：稍候重试
            if update:
                update(40, f"首次请求失败（{type(e).__name__}），2 秒后重试…")
            time.sleep(2)
            Path(out_path).unlink(missing_ok=True)
            seen.clear()
            try:
                asyncio.run(_run(seen))
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError(f"Edge 语音请求失败（已重试）: {e2}") from e
        if update:
            update(80, "转码为 wav…")
        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(out_path, wav)
        if update:
            update(100, "完成")
        return {"path": wav, "cost": 0.0, "chars": len(text)}


_engine = EdgeEngine()
