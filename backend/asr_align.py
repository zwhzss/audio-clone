# -*- coding: utf-8 -*-
"""整条音频识别 → 标点级文本单元 + 毫秒边界，写 JSON 给后端。

两个模型都在 F:\\GPT-SoVITS-main\\tools\\asr\\models 下：
- vad-punc paraformer：稳定、带时间戳，但不吃热词（这个 funasr 版本的普通 paraformer 推理阶段
  没有 hotword 分支）。
- seaco paraformer：推理阶段读 kwargs["hotword"]（models/seaco_paraformer/model.py:inference），
  所以给了热词就走它。

必须在 GPT-SoVITS 根目录下用 gpt_sovits 环境运行 —— app/slicing.align_units 负责这些；
单独跑时也一样，cwd 不对会把模型下载到错误位置（已在仓库里踩过 1.1GB 的坑）。

用法：
    python asr_align.py --in <音频> --out <json> [--hotword "姚氏 岳和 片瓦无存"]
"""
import argparse
import json
import os
import sys

ROOT = os.environ.get("GS_ROOT") or r"F:\GPT-SoVITS-main"
M = os.path.join(ROOT, "tools", "asr", "models")
VADPUNC = os.path.join(M, "speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
SEACO = os.path.join(M, "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
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


def _pick(hotword: str) -> str:
    if hotword and os.path.isdir(SEACO):
        return SEACO
    if os.path.isdir(VADPUNC):
        return VADPUNC
    if os.path.isdir(SEACO):
        return SEACO
    raise SystemExit(json.dumps({"fatal": "两个可输出时间戳的 paraformer 都没下载"}, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--hotword", default="")
    ap.add_argument("--device", default="")
    a = ap.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    for p in (VAD, PUNC):
        if not os.path.isdir(p):
            print(json.dumps({"fatal": "缺少模型目录: " + p}, ensure_ascii=False), flush=True)
            return 2
    model_dir = _pick(a.hotword.strip())
    used = os.path.basename(model_dir)

    from funasr import AutoModel

    dev = _device(a.device)
    m = AutoModel(model=model_dir, vad_model=VAD, punc_model=PUNC, disable_update=True, device=dev)
    kw = {"input": a.src, "sentence_timestamp": True}
    if a.hotword.strip() and "seaco" in used:
        kw["hotword"] = a.hotword.strip()
    res = m.generate(**kw)
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
    payload = {"source": os.path.abspath(a.src), "device": dev, "model": used,
               "hotwords": a.hotword.strip(), "units": units}
    # 缓存目录由调用方决定（分片任务用任务目录，数字人用 <数据目录>/_align），可能还不存在
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, a.out)
    print(json.dumps({"ok": True, "units": len(units), "model": used,
                      "hotwords": len(a.hotword.split()) if a.hotword.strip() else 0},
                     ensure_ascii=False), flush=True)
    return 0 if units else 1


if __name__ == "__main__":
    sys.exit(main())
