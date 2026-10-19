# -*- coding: utf-8 -*-
"""整条音频识别 → 标点级文本单元 + 毫秒边界，写 JSON 给后端。

用带时间戳的 vad-punc 变体（普通 paraformer 不输出 timestamp，会在 auto_model 里 KeyError）。
必须在 GPT-SoVITS 根目录下用 gpt_sovits 环境运行 —— app/slicing.align_units 负责这些，
单独跑时也一样：cwd 不对会把模型下载到错误位置（已在仓库里踩过 1.1GB 的坑）。

用法：
    F:\\envs\\gpt_sovits\\Scripts\\python.exe asr_align.py --in <音频> --out <json>
"""
import argparse
import json
import os
import sys

ROOT = os.environ.get("GS_ROOT") or r"F:\GPT-SoVITS-main"
M = os.path.join(ROOT, "tools", "asr", "models")
ASR = os.path.join(M, "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
VAD = os.path.join(M, "speech_fsmn_vad_zh-cn-16k-common-pytorch")
PUNC = os.path.join(M, "punc_ct-transformer_zh-cn-common-vocab272727-pytorch")


def _device(pref: str) -> str:
    if pref:
        return pref
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--device", default="")
    a = ap.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    for p in (ASR, VAD, PUNC):
        if not os.path.isdir(p):
            print(json.dumps({"fatal": "缺少模型目录: " + p}, ensure_ascii=False), flush=True)
            return 2

    from funasr import AutoModel

    dev = _device(a.device)
    m = AutoModel(model=ASR, vad_model=VAD, punc_model=PUNC, disable_update=True, device=dev)
    res = m.generate(input=a.src, sentence_timestamp=True)
    r0 = res[0] if isinstance(res, list) else res
    units = []
    for s in r0.get("sentence_info") or []:
        units.append({
            "text": s.get("text", "") or "",
            "start": round(float(s.get("start", 0)) / 1000, 3),
            "end": round(float(s.get("end", 0)) / 1000, 3),
            "chars": [[round(float(c[0]) / 1000, 3), round(float(c[1]) / 1000, 3)]
                      for c in (s.get("timestamp") or [])],
        })
    payload = {"source": os.path.abspath(a.src), "device": dev, "units": units}
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, a.out)
    print(json.dumps({"ok": True, "units": len(units), "out": a.out}, ensure_ascii=False), flush=True)
    return 0 if units else 1


if __name__ == "__main__":
    sys.exit(main())
