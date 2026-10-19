"""歌曲词曲分离引擎：UVR5（本地，GPT-SoVITS 自带）+ Demucs（本地，htdemucs）。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

GPT_SOVITS_ROOT = Path("F:/GPT-SoVITS-main")
PYTHON = r"F:\envs\gpt_sovits\Scripts\python.exe"

UVR5_MODELS = [
    {"id": "HP2_all_vocals", "name": "HP2 通用人声（MDX，推荐）", "desc": "通用流行歌曲人声/伴奏分离，效果好、速度快"},
    {"id": "model_bs_roformer_ep_317_sdr_12.9755", "name": "BS-Roformer（高质量）", "desc": "SDR 12.97 的先进模型，分离最干净但更慢"},
    {"id": "HP5_only_main_vocal", "name": "HP5 仅主唱", "desc": "侧重提取主唱人声，适合和声多的歌曲"},
    {"id": "VR-DeEchoNormal", "name": "VR-DeEcho 去回响", "desc": "带去回响处理，适合混响重的录音"},
]

DEMUCS_MODELS = [
    {"id": "htdemucs", "name": "htdemucs（Demucs 4）", "desc": "Meta 开源 SOTA 分离模型，人声/伴奏两轨输出"},
    {"id": "htdemucs_ft", "name": "htdemucs_ft（微调版）", "desc": "htdemucs 的微调版本，略好但更慢"},
]


def _env() -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "PYTHONPATH": str(GPT_SOVITS_ROOT) + os.pathsep + str(GPT_SOVITS_ROOT / "GPT_SoVITS"),
        "PATH": r"F:\envs\gpt_sovits\Library\bin;" + env.get("PATH", ""),
    })
    return env


def _run_cmd(args: list[str], update, msg: str, timeout: int = 7200) -> None:
    update(0, msg)
    proc = subprocess.Popen(
        args, cwd=str(GPT_SOVITS_ROOT), env=_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    logs: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            logs.append(line)
            if len(logs) > 40:
                logs.pop(0)
            update(None, line[-110:])
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(logs[-20:])
        raise RuntimeError(f"{msg} 失败 (exit {proc.returncode})\n{tail[-1200:]}")


def list_engines() -> list[dict]:
    return [
        {"id": "uvr5", "name": "UVR5（本地）", "models": UVR5_MODELS,
         "desc": "GPT-SoVITS 内置，MDX/Roformer 模型，免费离线", "cloud": False},
        {"id": "demucs", "name": "Demucs（本地）", "models": DEMUCS_MODELS,
         "desc": "Meta 开源 SOTA 模型（htdemucs），免费离线，首次运行自动下载模型", "cloud": False},
    ]


def separate(engine: str, model: str, input_path: str, out_dir: Path, update) -> dict:
    """执行分离，返回 {vocals, accompaniment}（相对 data 根的路径）。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 幂等恢复：主目录已有结果则直接复用（中断/重启后不重跑）
    v = out_dir / "vocals.wav"
    a = out_dir / "accompaniment.wav"
    if v.exists() and a.exists() and v.stat().st_size > 1000 and a.stat().st_size > 1000:
        update(90, "检测到已有分离结果，直接复用…")
        return _result(out_dir)

    if engine == "uvr5":
        _run_cmd(
            [str(PYTHON), "tools/uvr5/sep_cli.py",
             "--model", model, "--input", str(input_path),
             "--out_dir", str(out_dir), "--agg", "10", "--device", "cuda:0"],
            update, f"UVR5 分离中（{model}）…",
        )
    elif engine == "demucs":
        # Demucs 中断恢复：子目录已有输出则直接复制
        out_root = out_dir / "demucs_out"
        found_v = sorted(out_root.rglob("vocals.wav"))
        found_i = sorted(out_root.rglob("no_vocals.wav"))
        if found_v and found_i:
            update(60, "检测到上次分离的输出，直接复用…")
        else:
            _run_cmd(
                [str(PYTHON), "-m", "demucs", "--two-stems", "vocals",
                 "-n", model, "-o", str(out_root), str(input_path)],
                update, f"Demucs 分离中（{model}，首次需下载模型）…",
            )
            found_v = sorted(out_root.rglob("vocals.wav"))
            found_i = sorted(out_root.rglob("no_vocals.wav"))
        if not found_v or not found_i:
            raise RuntimeError("Demucs 输出缺失")
        import shutil

        shutil.copy2(found_v[0], out_dir / "vocals.wav")
        shutil.copy2(found_i[0], out_dir / "accompaniment.wav")
    else:
        raise RuntimeError(f"未知分离引擎: {engine}")

    if not v.exists() or not a.exists() or v.stat().st_size < 1000 or a.stat().st_size < 1000:
        raise RuntimeError("分离输出文件无效")
    return _result(out_dir)


def _result(out_dir: Path) -> dict:
    from .config import STORAGE

    rel = out_dir.relative_to(STORAGE["separation"]).as_posix() if out_dir.is_relative_to(STORAGE["separation"]) else out_dir.name
    return {
        "vocals": f"separation/{rel}/vocals.wav",
        "accompaniment": f"separation/{rel}/accompaniment.wav",
    }
