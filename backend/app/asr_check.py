"""ASR 校对：用常驻 sidecar 把合成产物回读成文字，与原文比字数。

为什么要它：GPT-SoVITS 的「丢句首/丢整句」不改变总时长的合理性 —— 实测 19 字的句子
只读出 14 字时耗时 4.38~5.62 秒，仍落在正常语速区间，靠时长判据完全看不出来。
字数比对是目前唯一可靠的判据。

不可用时一律返回 None（调用方必须跳过校验继续合成），绝不因为校对挂了而阻断产出。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

_WORD = re.compile(r"[\w\u3400-\u9fff]")
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_seq = 0
_sidecar_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "asr_sidecar.py")


def _python() -> str:
    from .gpt_sovits_train import PYTHON
    return str(PYTHON)


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _start():
    global _proc
    py = _python()
    if not os.path.exists(py) or not os.path.isfile(_sidecar_path):
        return None
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    libbin = os.path.join(os.path.dirname(os.path.dirname(py)), "Library", "bin")
    if os.path.isdir(libbin):
        env["PATH"] = libbin + os.pathsep + env.get("PATH", "")
    try:
        p = subprocess.Popen(
            [py, _sidecar_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
            cwd=os.path.dirname(_sidecar_path),
        )
    except Exception:
        return None
    try:
        # funasr/torchaudio 会在 stdout 打横幅，必须一直读到第一条 JSON 为止
        info = {}
        for _ in range(50):
            line = p.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                info = json.loads(line)
            except Exception:
                continue
            if info.get("ready") or info.get("fatal"):
                break
    except Exception:
        info = {}
    if not info.get("ready"):
        _kill(p)
        return None
    return p


def _kill(p):
    try:
        p.stdin.close()
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def available() -> bool:
    return os.path.exists(_python()) and os.path.isfile(_sidecar_path)


def transcribe(audio_path: str, timeout: int = 120) -> str | None:
    """把音频回读成文字。返回 None 表示校对不可用（调用方应跳过校验）。"""
    global _proc
    if not available() or not os.path.exists(audio_path):
        return None
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = _start()
            if _proc is None:
                return None
        wav = _to16k(audio_path)
        if wav is None:
            return None
        global _seq
        _seq += 1
        rid = _seq
        resp = {}
        try:
            _proc.stdin.write(json.dumps({"id": rid, "wav": wav}) + "\n")
            _proc.stdin.flush()
            for _ in range(50):
                line = _proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue          # 推理过程也可能往 stdout 打东西
                try:
                    resp = json.loads(line)
                except Exception:
                    continue
                if resp.get("id") == rid or resp.get("error"):
                    break
        except Exception:
            _kill(_proc)
            _proc = None
            return None
        finally:
            try:
                os.remove(wav)
            except Exception:
                pass
        if not resp or resp.get("error"):
            return None
        return resp.get("text") or ""


def _to16k(audio_path: str) -> str | None:
    """Paraformer 要 16k 单声道 wav。产物可能是 mp3 或其它采样率。"""
    out = os.path.join(tempfile.gettempdir(),
                       "asr_chk_%d_%d.wav" % (os.getpid(), threading.get_ident()))
    try:
        subprocess.run([_ffmpeg(), "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", out],
                       capture_output=True, timeout=60, check=True)
    except Exception:
        return None
    return out


def _fmt_seq(items) -> str:
    return "、".join(str(i) for i in (items or []))


_VERIFY_WORDS = re.compile(r"[\w一-鿿]")
# 丢字几乎都发生在这两类句子里：短句（模型倾向直接跳到句尾）和引号内的对话
# （实测 19 字的对话引导句 4/5 次被吃开头，而 40~67 字的长叙述句都完整）。
_RISKY_QUOTE = re.compile(r"[“”‘’\"「『]")
_SHORT_CHARS = 16

_MODES = ("full", "smart", "off")


def current_mode() -> str:
    """校验模式：设置页 > .env(ASR_VERIFY) > 默认 smart。

    full  = 每句都校验（最准，每段最多多花 5 次生成）
    smart = 只校验短句和含引号的对话句（丢字的重灾区），长叙述句只查时长
    off   = 完全退回旧的「只看总时长」判据
    """
    from .config import ASR_VERIFY_MODE

    mode = ASR_VERIFY_MODE
    try:
        from .db import SessionLocal
        from .models import get_setting

        db = SessionLocal()
        try:
            v = (get_setting(db, "asr_verify_mode", "") or "").strip().lower()
        finally:
            db.close()
        if v in _MODES:
            mode = v
    except Exception:
        pass
    return mode


def should_verify(sent: str, mode: str | None = None) -> bool:
    mode = mode or current_mode()
    if mode == "off" or not available():
        return False
    if mode == "full":
        return True
    n = len(_VERIFY_WORDS.findall(sent or ""))
    return n <= _SHORT_CHARS or bool(_RISKY_QUOTE.search(sent or ""))


def format_note(r: dict) -> str:
    """把合成结果里的校验信息写成一句人话，供项目分段与单次合成共用。"""
    fb = r.get("fallback_sentences") or []
    dg = r.get("degraded_sentences") or []
    mw = r.get("missing_words") or {}
    parts = []
    if fb:
        parts.append(f"第 {_fmt_seq(fb)} 句原模型内容不全，已用官方语义模型补合成（音色一致，节奏可能略不同）")
    if dg:
        detail = "、".join(f"第 {k} 句缺「{'，'.join(v)}」"
                          for k, v in sorted(mw.items(), key=lambda x: int(x[0]))) if mw else ""
        parts.append(f"第 {_fmt_seq(dg)} 句换模型后仍缺字{('：' + detail) if detail else ''}，"
                     f"建议改写这一段或换一条更完整的参考音频")
    return "；".join(parts)


def compare(expected: str, audio_path: str) -> dict | None:
    """比校对结果与原文的实义字数。返回 None 表示无法校对。

    ASR 本身会听错同音字（话音未落→万音微弱），所以判据只看字数，不看措辞。
    """
    exp = "".join(_WORD.findall(expected or ""))
    if not exp:
        return {"ok": True, "expected": 0, "heard": 0, "missing": [], "ratio": 1.0}
    got = transcribe(audio_path)
    if got is None:
        return None
    act = "".join(_WORD.findall(got))
    ratio = len(act) / len(exp)
    import difflib
    missing = []
    sm = difflib.SequenceMatcher(a=exp, b=act)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and i2 - i1 >= 2:
            missing.append(exp[i1:i2])
    return {"ok": ratio >= 0.90, "expected": len(exp), "heard": len(act),
            "missing": missing, "ratio": round(ratio, 2)}
