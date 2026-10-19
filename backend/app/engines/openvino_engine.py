"""OpenVINO 引擎：NPU/CPU 端侧本地推理（openvino-genai TTS）。

不需要联网、不占 GPU、零费用。需自行准备 OpenVINO 模型（推荐 MMS/VITS 系列中文模型）：
    pip install openvino openvino-genai optimum[openvino]
    optimum-cli export openvino --model facebook/mms-tts-zho E:/models/mms-tts-zho-ov
在「设置」页填写模型目录与设备（NPU/CPU/GPU），或 .env 配置 OPENVINO_MODEL_DIR / OPENVINO_DEVICE。
注意：该引擎为固定音色合成（无克隆），适合初版音频与隐私敏感的本地合成。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..audio_utils import audio_duration, convert_to_wav
from ..models import Voice, get_active_model, get_setting
from .base import BaseEngine, ProgressCb

# 进程级缓存：避免每次合成重复加载模型（模型加载耗时数秒）
_pipeline_cache: dict[str, object] = {}
_pipeline_lock = __import__("threading").Lock()


def _load_pipeline(model_dir: str, device: str):
    key = f"{model_dir}|{device}"
    with _pipeline_lock:
        if key not in _pipeline_cache:
            import openvino_genai  # noqa: F401  懒加载

            _pipeline_cache[key] = openvino_genai.TextToSpeechPipeline(model_dir, device)
        return _pipeline_cache[key]


class OpenVINOEngine(BaseEngine):
    id = "openvino"
    name = "OpenVINO（NPU 端侧）"
    desc = "本地 NPU/CPU 推理 TTS：免联网、免费用、不占 GPU；需准备 OpenVINO 模型（MMS/VITS 系）"
    supports_clone = False
    supports_vc = False
    is_cloud = False
    needs_key = False
    cost_hint = "免费（本地端侧推理）"

    def _model(self, db) -> dict | None:
        """从模型库读取激活的 OpenVINO 模型（含 settings 兼容回退）。"""
        m = get_active_model(db, "voice")
        if m is not None and m.provider == "openvino":
            extra = m.extra or {}
            return {
                "model_dir": extra.get("model_dir", ""),
                "device": extra.get("device", "NPU"),
                "max_len": int(float(extra.get("max_audio", "30"))),
            }
        model_dir = get_setting(db, "openvino_model_dir", "")
        if model_dir:
            return {
                "model_dir": model_dir,
                "device": get_setting(db, "openvino_device", "NPU"),
                "max_len": int(float(get_setting(db, "openvino_max_audio", "30"))),
            }
        return None

    def check(self, db) -> dict:
        m = self._model(db)
        active = get_active_model(db, "voice")
        if m is None:
            if active is not None and active.provider != "openvino":
                return {
                    "ok": False,
                    "message": "未激活 OpenVINO（当前语音模型：%s）" % active.name,
                    "detail": "在「模型库」中激活 OpenVINO 条目并配置模型目录",
                }
            return {"ok": False, "message": "未配置模型目录", "detail": "在「模型库」中激活 OpenVINO 并填写模型目录"}
        model_dir = m["model_dir"]
        device = m["device"]
        if not model_dir:
            return {"ok": False, "message": "未配置模型目录", "detail": "在模型库中为 OpenVINO 条目填写模型目录（extra.model_dir）"}
        md = Path(model_dir)
        if not (md / "openvino.xml").exists():
            return {"ok": False, "message": "模型文件缺失", "detail": f"{md} 下未找到 openvino.xml"}
        try:
            import openvino_genai  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "message": "未安装 openvino-genai",
                "detail": "pip install openvino openvino-genai optimum[openvino]",
            }
        return {"ok": True, "message": f"模型就绪（{device}）", "detail": str(md)}

    def list_voices(self, db) -> list[dict]:
        m = self._model(db)
        if not m or not m["model_dir"]:
            return []
        name = "NPU 本地音色"
        cfg_file = Path(m["model_dir"]) / "voice.json"
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                name = cfg.get("name", name)
            except Exception:  # noqa: BLE001
                pass
        return [{"id": f"openvino:{m['model_dir']}", "name": name, "lang": "zh"}]

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            m = self._model(db)
            if m is None:
                raise RuntimeError("未激活 OpenVINO 语音模型（模型库）")
            model_dir, device, max_len = m["model_dir"], m["device"], m["max_len"]
        finally:
            db.close()
        if not model_dir:
            raise RuntimeError("OpenVINO 模型目录未配置（模型库）")

        if update:
            update(20, "加载端侧模型…")
        try:
            pipe = _load_pipeline(model_dir, device)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"模型加载失败（{device}）: {e}") from e

        if update:
            update(60, f"NPU 推理中（{device}）…")
        try:
            import numpy as np

            audio = pipe.generate(text, max_audio_length=max_len)
            sr = int(getattr(pipe, "sample_rate", 16000))
            arr = np.asarray(audio)
            if arr.dtype.kind == "f":
                arr = np.clip(arr, -1.0, 1.0)
                arr = (arr * 32767).astype(np.int16)
            arr = arr.astype(np.int16)
            # 写 wav
            import wave

            wav_raw = str(Path(out_path).with_suffix(".raw.wav"))
            with wave.open(wav_raw, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(arr.tobytes())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"NPU 推理失败: {e}") from e

        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(wav_raw, wav)
        Path(wav_raw).unlink(missing_ok=True)
        if update:
            update(100, "完成")
        return {"path": wav, "cost": 0.0, "chars": len(text), "duration": audio_duration(wav)}


_engine = OpenVINOEngine()
