"""GPT-SoVITS 引擎：本地 GPU 声音克隆（零样本克隆 + 可选微调 + 音色转换 VC）。

需要用户自行安装 GPT-SoVITS 并启动 api_v2 服务（默认 http://127.0.0.1:9880），
参考音频目录（GPT_SOVITS_REF_DIR）需本服务与 GPT-SoVITS 都能访问。
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from ..audio_utils import audio_duration, convert_to_wav
from .. import asr_check as _asr
from ..config import (
    GPT_SOVITS_BASE_GPT,
    GPT_SOVITS_BASE_SOVITS,
    GPT_SOVITS_REF_DIR,
    GPT_SOVITS_URL,
)
from ..models import Voice
from ..text_utils import prepare_tts_text, split_sentences
from .base import BaseEngine, ProgressCb

# api_v2 的 set_*_weights 每次都无条件重载模型，故记录本进程已生效的权重路径，
# 路径未变则跳过。api_v2 会把权重路径写进 tts_infer.yaml，重启后仍与上次一致，
# 因此该缓存与真实状态保持同步；仅当有人绕过本服务改权重时才会失效。
_loaded_weights: dict[str, dict[str, str]] = {}


class GPTSoVITSEngine(BaseEngine):
    id = "gpt_sovits"
    name = "GPT-SoVITS（本地 GPU）"
    desc = "本地开源克隆引擎：10 秒参考音频零样本克隆，支持音色转换（初版音频换声），支持微调模型"
    supports_clone = True
    supports_vc = True
    is_cloud = False
    needs_key = False
    cost_hint = "免费（本地 GPU 推理）"

    # ---------- 工具 ----------

    def _client(self, timeout: float = 600.0) -> httpx.Client:
        return httpx.Client(base_url=GPT_SOVITS_URL.rstrip("/"), timeout=timeout)

    def _server_ref_dir(self) -> Path:
        if not GPT_SOVITS_REF_DIR:
            raise RuntimeError(
                "未配置 GPT_SOVITS_REF_DIR（GPT-SoVITS 服务端可访问的参考音频目录）。"
                "请在 .env 中设置，例如 GPT_SOVITS_REF_DIR=E:/GPT-SoVITS/refs"
            )
        d = Path(GPT_SOVITS_REF_DIR)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _push_audio_to_server(self, local_path: str, start: float = 0.0, clip: float | None = None) -> str:
        """把本地音频转成 wav 后复制到 GPT-SoVITS 服务端可见目录，返回服务端路径。

        必须转 wav：GPT-SoVITS 用 soundfile/torchaudio 解码参考音频，不支持 m4a/aac 等格式。
        start/clip 指定截取区间（参考音频需 3~10 秒）。
        """
        from ..audio_utils import extract_wav

        ref_dir = self._server_ref_dir()
        wav_tmp = str(Path(local_path).with_suffix(".push_tmp.wav"))
        if clip:
            extract_wav(local_path, wav_tmp, start=start, duration=clip)
        else:
            extract_wav(local_path, wav_tmp, start=start)
        name = f"vo_{uuid.uuid4().hex[:8]}.wav"
        dst = ref_dir / name
        shutil.copy2(wav_tmp, dst)
        Path(wav_tmp).unlink(missing_ok=True)
        return str(dst)

    def _align_ref(self, ref_text: str, dur: float) -> tuple[float, float, str]:
        """把参考文本按句子映射到音频时间轴，选出 3~10 秒的一组完整句子。

        返回 (start_秒, clip_秒, 对齐后的参考文本)。
        参考音频与 prompt_text 不对应会导致合成时混入素材内容。
        """
        import re

        # GPT-SoVITS 用 librosa.load(sr=16000) 读参考音频并要求 48000 <= len <= 160000
        # （TTS.py:_set_prompt_semantic），即严格 3~10 秒。重采样会让样本数略微溢出，
        # 所以取满 10.0 秒会被判"参考音频在3~10秒范围外"，必须留出余量。
        SAFE_MAX = 9.0
        # 英文句号也要作为切分点，否则整段英文被当成一句：会取前 SAFE_MAX 秒音频
        # 却配完整文本，造成 prompt_text 与参考音频不匹配。小数（如 3.5）不切。
        sents = [s.strip() for s in re.split(r"(?<=[。！？；!?;])|(?<=[^0-9])\.(?![0-9])|(?<=[0-9])\.(?=[^0-9])",
                                             ref_text or "") if s.strip()]
        if not sents:
            return 0.0, min(dur, SAFE_MAX), ref_text
        if len(sents) == 1:
            return 0.0, min(dur, SAFE_MAX), ref_text
        total_chars = max(1, len(ref_text))
        fracs = [len(s) / total_chars for s in sents]
        # 找一段连续的完整句子，估算时长最接近 6.5 秒（3~10 秒内；参考音频越短越不易混入素材）
        best = None
        n = len(sents)
        for start_i in range(n):
            acc = 0.0
            for end_i in range(start_i, n):
                acc += fracs[end_i] * dur
                if acc > SAFE_MAX:
                    break
                if acc >= 3.0 and (best is None or abs(acc - 6.5) < abs(best[2] - 6.5)):
                    best = (start_i, end_i, acc)
        if best is None:
            # 单句就超上限：取第一句开头 SAFE_MAX 秒
            return 0.0, SAFE_MAX, sents[0]
        start_i, end_i, acc = best
        start_frac = sum(fracs[:start_i])
        new_text = "".join(sents[start_i:end_i + 1])
        return start_frac * dur, acc, new_text

    def _check_audio_ok(self, resp: httpx.Response) -> bytes:
        ctype = resp.headers.get("content-type", "")
        # 非 200：优先解析 JSON 错误并透传真实原因
        if resp.status_code != 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("msg") or data.get("detail") or resp.text[:300]
                    exc = data.get("Exception") or data.get("exception") or ""
                    raise RuntimeError(f"GPT-SoVITS 错误 (HTTP {resp.status_code}): {msg} {exc}".strip())
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"GPT-SoVITS 错误 (HTTP {resp.status_code}): {resp.text[:300]}")
        if "audio" in ctype or resp.content[:4] == b"RIFF":
            return resp.content
        # 200 但非音频：可能是 JSON 包装（某些版本 {code,msg,audio(base64)}）
        try:
            data = resp.json()
            if isinstance(data, dict):
                if data.get("code") not in (0, None):
                    raise RuntimeError(f"GPT-SoVITS 错误: {data.get('msg', data)}")
                b64 = data.get("audio")
                if b64:
                    return base64.b64decode(b64)
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"GPT-SoVITS 返回异常 (HTTP {resp.status_code}): {resp.text[:300]}")

    # ---------- 引擎接口 ----------

    def check(self, db) -> dict:
        try:
            with self._client(5.0) as c:
                r = c.get("/docs")  # api_v2 无根路由，用 /docs 探测
                ok = r.status_code == 200
                return {
                    "ok": ok,
                    "message": "服务在线" if ok else f"HTTP {r.status_code}",
                    "detail": GPT_SOVITS_URL,
                }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "message": "未检测到 GPT-SoVITS 服务",
                "detail": (
                    f"{e}。请先启动 GPT-SoVITS API 服务（api_v2，默认端口 9880），"
                    "并在 .env 中配置 GPT_SOVITS_URL 与 GPT_SOVITS_REF_DIR"
                ),
            }

    def clone(self, voice: Voice, db, update: ProgressCb | None = None) -> dict:
        """零样本克隆：参考音频对齐文本后放到服务端目录并校验。"""
        if update:
            update(10, "检查 GPT-SoVITS 服务…")
        info = self.check(db)
        if not info["ok"]:
            raise RuntimeError(info["detail"])
        if not voice.ref_audio or not Path(voice.ref_audio).exists():
            raise RuntimeError("缺少参考音频")
        if update:
            update(30, "分析参考音频…")
        from ..audio_utils import audio_duration

        dur = audio_duration(voice.ref_audio)
        if dur < 3:
            raise RuntimeError("参考音频过短（<3 秒），建议 5~10 秒清晰人声")
        # 音频与参考文本按句子对齐（防止合成时混入素材内容）
        start, clip, prompt_text = self._align_ref(voice.ref_text, dur)
        if update:
            update(50, f"截取参考片段 {clip:.1f} 秒并上传服务端…")
        ref_path = self._push_audio_to_server(voice.ref_audio, start=start, clip=clip)
        if update:
            update(80, "校验音频…")
        cfg = dict(voice.engine_config or {})
        cfg["ref_path"] = ref_path
        cfg["ref_text"] = prompt_text or (voice.ref_text or "")
        cfg["ref_lang"] = voice.ref_lang or "zh"
        cfg["zero_shot"] = True
        cfg["ref_clip"] = {"start": round(start, 2), "duration": round(clip, 2)}
        # 仅未微调的声音才锚定官方底模；已微调的必须保留其专属权重，
        # 否则「重新克隆」会把微调模型覆盖掉。
        if not cfg.get("finetuned"):
            cfg["gpt_model"] = GPT_SOVITS_BASE_GPT
            cfg["sovits_model"] = GPT_SOVITS_BASE_SOVITS
        if update:
            update(100, "克隆就绪（零样本模式）")
        return {"model_id": "zero-shot", "engine_config": cfg}

    def _aux_ref_paths(self, voice: Voice) -> list[str]:
        """音色混合：读取 voice.engine_config 中的
        - aux_refs: 其他克隆声音的 voice_id 列表（其服务端 ref_path 直接可用）
        - aux_ref_paths: 任意本地音频路径列表（如数字人训练数据切片），push 到服务端
        返回全部服务端参考音频路径（多参考音色融合，形成独特声音）。"""
        cfg = voice.engine_config or {}
        aux_ids = cfg.get("aux_refs") or []
        aux_paths = cfg.get("aux_ref_paths") or []
        if not aux_ids and not aux_paths:
            return []
        from ..db import SessionLocal
        from ..models import Voice as VoiceModel
        paths: list[str] = []
        db = SessionLocal()
        try:
            for vid in aux_ids:
                if vid == voice.id:
                    continue
                v = db.get(VoiceModel, vid)
                if v and v.engine_config and v.engine_config.get("ref_path"):
                    paths.append(v.engine_config["ref_path"])
        finally:
            db.close()
        for p in aux_paths:
            if not p or not str(p).strip():
                continue
            if str(p) == cfg.get("ref_path"):
                continue  # 不与主参考重复
            if Path(p).exists():
                paths.append(self._push_audio_to_server(str(p)))
        return paths

    def _prepare(self, voice: Voice):
        """应用微调模型权重（若配置了 gpt_model/sovits_model）。"""
        from urllib.parse import quote

        cfg = voice.engine_config or {}
        want = {"gpt": cfg.get("gpt_model", ""), "sovits": cfg.get("sovits_model", "")}
        cur = _loaded_weights.setdefault(GPT_SOVITS_URL, {"gpt": "", "sovits": ""})
        endpoints = {"gpt": "/set_gpt_weights", "sovits": "/set_sovits_weights"}
        with self._client() as c:
            for kind in ("gpt", "sovits"):
                path = want[kind]
                if not path or path == cur[kind]:
                    continue
                r = c.get(f"{endpoints[kind]}?weights_path={quote(path, safe='')}")
                if r.status_code != 200:
                    label = "GPT" if kind == "gpt" else "SoVITS"
                    raise RuntimeError(f"加载 {label} 权重失败: {r.text[:300]}")
                cur[kind] = path

    def _swap_gpt_weights(self, path: str, warm_payload: dict | None = None) -> None:
        """切换 s1（语义/韵律）权重，并丢弃切换后必塌的第一次调用。

        api_v2 的 /set_gpt_weights 无条件重载模型，重载后第一次合成固定返回约
        0.74 秒的废音频（ASR 回读为 0 字）。暖机必须用**完整可用的请求体**
        （含 ref_audio_path/prompt_text），否则服务端直接 400，塌方会落在真实句子上。
        """
        from urllib.parse import quote

        cur = _loaded_weights.setdefault(GPT_SOVITS_URL, {"gpt": "", "sovits": ""})
        if cur["gpt"] == path:
            return
        with self._client() as c:
            r = c.get(f"/set_gpt_weights?weights_path={quote(path, safe='')}")
        if r.status_code != 200:
            raise RuntimeError(f"加载 GPT 权重失败: {r.text[:300]}")
        cur["gpt"] = path
        if warm_payload:
            try:
                with self._client() as c:
                    c.post("/tts", json=warm_payload)
            except Exception:  # noqa: BLE001 - 暖机失败不影响主流程，只是少丢一次废结果
                pass

    def tts(self, voice: Voice, text: str, out_path: str, update: ProgressCb | None = None) -> dict:
        cfg = voice.engine_config or {}
        ref_path = cfg.get("ref_path", "")
        is_finetuned = cfg.get("finetuned", False)
        if not ref_path and not is_finetuned:
            raise RuntimeError("该声音尚未完成克隆，请先在「声音库」执行克隆")
        if update:
            update(20, "准备 GPT-SoVITS 推理…")
        self._prepare(voice)
        if update:
            update(50, "推理中（本地 GPU）…")
        aux_refs = self._aux_ref_paths(voice)
        # ASR 校对需要 F:\envs\gpt_sovits 环境与本地模型；不可用时自动退回时长判据
        vmode = _asr.current_mode()
        can_verify = vmode != "off" and _asr.available()

        # 按句子切分，每句单独合成（防止模型跨句吞字）。必须用引号感知的分句器：
        # 直接按 。！？ 硬切会把 `“站住！什么人？”` 拆成两次调用，对话读起来断裂，
        # 还会产生只含 `”` 的碎片送给模型（约 0.7 秒噪声）。
        sentences = split_sentences(text)
        if len(sentences) <= 1:
            sentences = [text.strip()]

        all_audio = []
        total = len(sentences)
        seg_dir = Path(out_path).parent
        concat_list = seg_dir / "_concat.txt"
        paths = [seg_dir / f"_sent_{i}.wav" for i in range(total)]
        import random

        content_re = re.compile(r"[\w\u3400-\u9fff]")

        def payload(sent: str, seed: int | None = None) -> dict:
            d = {
                "text": prepare_tts_text(sent),
                "text_lang": cfg.get("ref_lang", "auto"),
                "ref_audio_path": ref_path,
                "aux_ref_audio_paths": aux_refs,
                "prompt_text": cfg.get("ref_text", ""),
                "prompt_lang": cfg.get("ref_lang", "zh"),
                "top_k": int(cfg.get("top_k", 15)),
                "top_p": float(cfg.get("top_p", 0.85)),
                "temperature": float(cfg.get("temperature", 0.72)),
                # cut5 会在每个逗号处切开，每块各自带首尾静音，拼接后静音累加
                # 实测 51 字文本静音占比 39%（最长间隙 1.5s），听感明显断续。
                # 引擎已按句预切，服务端只需按句号再切，故用 cut3。
                "text_split_method": "cut3",
                "batch_size": 1,
                "speed_factor": 1.0,
                "stream": False,
                "media_type": "wav",
            }
            if seed is not None:
                d["seed"] = seed
            return d

        def synth(idx: int, sent: str, attempts: int, incumbent: dict | None = None,
                  verify: bool = True) -> dict:
            """合成第 idx 句，返回 {ok, dur, missing}。

            验收标准是 **ASR 回读的字数比**，不是时长：实测 19 字只读出 13 字时耗时
            4.4~5.6 秒，语速看着完全正常，时长判据对「丢句首/丢整句」是瞎的。
            ASR 不可用时自动退回时长判据，绝不因为校对挂了而阻断合成。
            多次尝试保留字数比最高的一次（不是最后一次）。
            """
            n_content = len(content_re.findall(sent)) or len(sent)
            want = max(0.6, n_content * 0.4) * 0.55
            # incumbent = 上一遍（例如微调模型那遍）已落盘的结果；回退补合成时
            # 只有**更好**才覆盖它，否则会把能听的版本换成更差的。
            best = {"score": -1.0, "dur": 0.0, "missing": []}
            if incumbent:
                best = {"score": incumbent.get("score", -1.0), "dur": incumbent.get("dur", 0.0),
                        "missing": incumbent.get("missing") or [], "incumbent": True}
            for attempt in range(attempts):
                data = payload(sent, None if attempt == 0 else random.randint(0, 2 ** 31 - 1))
                with self._client() as c:
                    r = c.post("/tts", json=data)
                audio = self._check_audio_ok(r)
                if not audio or len(audio) < 44:
                    continue
                tmp = paths[idx].with_name("_try_%d.wav" % idx)
                tmp.write_bytes(audio)
                d = audio_duration(str(tmp))
                v = _asr.compare(sent, str(tmp)) if verify else None
                if v is None:
                    score, missing, ok = min(1.0, d / max(want, 0.01)), [], d >= want
                else:
                    score, missing = v["ratio"], v["missing"]
                    ok = v["ok"] and d >= want * 0.7
                if score > best["score"]:          # 只保留最优一次，其余落盘即删
                    paths[idx].unlink(missing_ok=True)
                    tmp.replace(paths[idx])
                    best = {"score": score, "dur": d, "missing": missing}
                else:
                    tmp.unlink(missing_ok=True)
                if ok:
                    return {"ok": True, "dur": d, "missing": missing, "score": score}
            return {"ok": False, **best}

        try:
            failed = []
            prev, lost = {}, {}
            for idx, sent in enumerate(sentences):
                if update:
                    update(50 + int(idx / total * 40), f"合成第 {idx + 1}/{total} 段…")
                check = can_verify and _asr.should_verify(sent, vmode)
                r1 = synth(idx, sent, 3, verify=check)
                prev[idx] = r1
                lost[idx] = r1.get("missing") or []
                if not r1["ok"]:
                    failed.append(idx)

            fallback, degraded = [], []
            own = cfg.get("gpt_model", "")
            can_fallback = bool(own and failed and Path(GPT_SOVITS_BASE_GPT).exists()
                                and own != GPT_SOVITS_BASE_GPT)
            if failed and not can_fallback:
                degraded = [i + 1 for i in failed]
            elif can_fallback:
                # 内容缺失是微调 s1 的残留问题（base s1 同句正常）：换成官方 s1
                # 只补合成这几句，s2 不变所以音色一致；代价是节奏略不一致。
                if update:
                    update(70, f"{len(failed)} 句内容缺失，改用官方语义模型补合成…")
                self._swap_gpt_weights(GPT_SOVITS_BASE_GPT, payload("好。"))
                try:
                    for idx in failed:
                        r2 = synth(idx, sentences[idx], 2, incumbent=prev[idx],
                                   verify=can_verify)
                        if r2["ok"]:
                            fallback.append(idx + 1)
                        else:
                            degraded.append(idx + 1)
                        if len(r2.get("missing") or []) < len(lost.get(idx) or []):
                            lost[idx] = r2.get("missing") or []
                finally:
                    self._swap_gpt_weights(own, payload("好。"))

            missing_words = {}
            for idx in degraded:
                if lost.get(idx):
                    missing_words[idx + 1] = lost[idx]

            silent = [i + 1 for i in range(total) if not paths[i].exists()]
            if silent:
                raise RuntimeError(
                    f"第 {silent} 句合成返回空音频（重试与官方模型回退均失败），请缩短该句或换参考音频")
            all_audio.extend(str(p) for p in paths)

            # 用 FFmpeg concat 正确拼接（保留每个文件的 WAV 头部）
            if len(all_audio) == 1:
                shutil.copy2(all_audio[0], out_path)
            else:
                concat_list.write_text("\n".join(f"file '{p}'" for p in all_audio))
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                # 显式 -f wav：out_path 扩展名可能是 .mp3，若不指定格式 ffmpeg 会用
                # mp3 muxer + -c copy（PCM 流）而失败（Invalid audio stream）
                r = subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                     "-f", "wav", "-c", "copy", str(out_path)],
                    capture_output=True, timeout=120,
                )
                if r.returncode != 0:
                    raise RuntimeError(f"音频拼接失败: {r.stderr.decode('utf-8', 'ignore')[-300:]}")
        finally:
            # out_path.parent 是本次调用独占的目录，逐句临时文件在此统一回收；
            # 用 glob 而非 all_audio，保证中途抛错时也不残留。
            for p in list(seg_dir.glob("_sent_*.wav")) + list(seg_dir.glob("_try_*.wav")):
                p.unlink(missing_ok=True)
            concat_list.unlink(missing_ok=True)
        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(out_path, wav)
        if degraded and update:
            update(98, f"注意：第 {degraded} 句字数仍不足，可能有丢字")
        if update:
            update(100, "完成")
        return {"path": wav, "cost": 0.0, "chars": len(text), "duration": audio_duration(wav),
                "fallback_sentences": sorted(fallback), "degraded_sentences": sorted(degraded),
                "missing_words": missing_words, "verify_mode": vmode}

    def voice_convert(self, voice: Voice, input_audio: str, out_path: str, update: ProgressCb | None = None) -> dict:
        """音色转换：初版音频 → 克隆音色。"""
        cfg = voice.engine_config or {}
        ref_path = cfg.get("ref_path", "")
        if not ref_path:
            raise RuntimeError("该声音尚未完成克隆")
        if update:
            update(20, "上传初版音频到服务端…")
        input_path = self._push_audio_to_server(input_audio)
        if update:
            update(50, "音色转换中（本地 GPU）…")
        data = {
            "input_audio_path": input_path,
            "tgt_speaker_wav": ref_path,
            "tgt_speaker_text": cfg.get("ref_text", ""),
            "tgt_speaker_language": cfg.get("ref_lang", "zh"),
            "top_k": int(cfg.get("top_k", 5)),
            "top_p": float(cfg.get("top_p", 1.0)),
            "temperature": float(cfg.get("temperature", 1.0)),
            "speed_factor": 1.0,
        }
        with self._client() as c:
            r = c.post("/vc", data=data)
        audio = self._check_audio_ok(r)
        Path(out_path).write_bytes(audio)
        wav = str(Path(out_path).with_suffix(".wav"))
        convert_to_wav(out_path, wav)
        if update:
            update(100, "完成")
        return {"path": wav, "cost": 0.0, "duration": audio_duration(wav)}


_engine = GPTSoVITSEngine()
