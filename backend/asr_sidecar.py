# -*- coding: utf-8 -*-
"""ASR sidecar：常驻进程，加载 Paraformer 一次，按行读 JSON 请求、回 JSON 结果。

由 backend/app/asr_check.py 用 F:\\envs\\gpt_sovits 的 python 启动（后端 venv 里没有 torch，
也不该装），协议是 stdin/stdout 每行一个 JSON：
    请求  {"wav": "绝对路径", "id": 1}
    响应  {"id": 1, "text": "识别文本"}
    失败  {"id": 1, "error": "..."}
ready 之前会先输出一行 {"ready": true}，父进程据此确认加载完成。
"""
import json
import os
import sys
import traceback

ROOT = r"F:\GPT-SoVITS-main"
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from funasr import AutoModel  # noqa: E402

MODEL = os.path.join(ROOT, "tools/asr/models/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")

if not os.path.isdir(MODEL):
    print(json.dumps({"fatal": "本地缺少 paraformer 模型目录: %s" % MODEL}), flush=True)
    sys.exit(1)

try:
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
except Exception:
    device = "cpu"

try:
    model = AutoModel(model=MODEL, disable_update=True, device=device)
except Exception:
    print(json.dumps({"fatal": "加载 ASR 模型失败: %s" % traceback.format_exc()[-400:]}), flush=True)
    sys.exit(1)

print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    rid = req.get("id")
    wav = req.get("wav", "")
    if not os.path.exists(wav):
        print(json.dumps({"id": rid, "error": "文件不存在"}, ensure_ascii=False), flush=True)
        continue
    try:
        text = model.generate(input=wav)[0].get("text", "")
        print(json.dumps({"id": rid, "text": text}, ensure_ascii=False), flush=True)
    except Exception:
        print(json.dumps({"id": rid, "error": traceback.format_exc()[-200:]}, ensure_ascii=False),
              flush=True)
