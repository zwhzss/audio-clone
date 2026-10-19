"""训练流水线端到端验证：edge 生成测试语音 → 上传 → 训练（最小轮次）→ 微调模型合成。"""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def req(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=600) as resp:
        raw = resp.read()
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return json.loads(raw.decode("utf-8"))
        return {"_raw": raw}


def wait_job(job_id, timeout=7200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        s = req("GET", f"/api/jobs/{job_id}")
        if s["status"] not in ("queued", "running"):
            return s
    return {"status": "timeout"}


def main():
    # 1. 生成测试训练语音（edge 合成 8 段短句，模拟录音）
    v_edge = req("POST", "/api/voices", {"name": "训练数据生成", "source": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
    sentences = [
        "大家好，欢迎收听我的声音。",
        "今天天气很好，我们出去走走吧。",
        "这本书非常有意思，推荐给大家。",
        "生活就像一杯茶，慢慢品味才有滋味。",
        "清晨的阳光洒在窗台上，格外温暖。",
        "我喜欢在夜晚读书，安静又专注。",
        "旅行的意义，在于遇见不同的风景。",
        "愿你每一天都充满希望和快乐。",
        "雨后的天空格外清澈，空气里带着泥土的芬芳。",
        "站在山顶眺望远方，群山连绵起伏，让人心旷神怡。",
    ]
    audios = []
    for i, s in enumerate(sentences):
        t = req("POST", "/api/synthesis/tts", {"voice_id": v_edge["id"], "text": s, "title": f"训练{i}"})
        st = wait_job(t["job_id"], timeout=600)
        if st["status"] == "success":
            audios.append(st["result"]["path"])
    check("生成 10 段训练语音", len(audios) == 10, f"({len(audios)})")
    req("DELETE", f"/api/voices/{v_edge['id']}")

    # 2. 创建 GPT-SoVITS 声音（用之前的克隆声音）
    voice_id = "b53ba76448e3"  # 用户的「我的声音克隆」
    v = req("GET", f"/api/voices/{voice_id}")
    check("目标声音存在", v["id"] == voice_id)

    # 3. 上传训练音频（zip）
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, rel in enumerate(audios):
            p = os.path.join(r"E:\go-project\src\audio-clone\data", rel.replace("/", "\\"))
            z.write(p, f"train_{i:02d}.wav")
    buf.seek(0)
    boundary = "----tr"
    data = buf.read()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"train.zip\"\r\nContent-Type: application/zip\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    m = urllib.request.Request(BASE + f"/api/voices/{voice_id}/train-audio", data=b"".join(parts), method="POST")
    m.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(m, timeout=300) as resp:
        up = json.loads(resp.read().decode())
    check("上传训练数据", up["files"] >= 8, f"({up['files']} 个, {up['duration']}s)")

    # 4. 发起训练（最小轮次验证流程）
    t = req("POST", f"/api/voices/{voice_id}/train", {"speaker": "myvoice", "s2_epochs": 2, "s1_epochs": 2})
    check("训练任务已提交", bool(t.get("job_id")))
    print("  训练开始，预计 20-60 分钟（ASR 首次需下载模型）…")

    s = wait_job(t["job_id"], timeout=7200)
    check("训练完成", s["status"] == "success", f"({s.get('message')})")
    if s["status"] != "success":
        print("训练失败详情:", str(s.get("result", {}).get("traceback", ""))[-1500:])
        return

    # 5. 验证微调模型已记录
    v2 = req("GET", f"/api/voices/{voice_id}")
    cfg = v2["engine_config"]
    check("微调模型已记录", bool(cfg.get("sovits_model")) and bool(cfg.get("gpt_model")), f"(finetuned={cfg.get('finetuned')})")
    import os as _os

    check("模型文件存在", _os.path.exists(cfg["sovits_model"]) and _os.path.exists(cfg["gpt_model"]))
    print(f"  sovits: {cfg['sovits_model']}")
    print(f"  gpt:    {cfg['gpt_model']}")

    # 6. 微调模型合成测试（任意文本）
    t = req("POST", "/api/synthesis/tts", {
        "voice_id": voice_id,
        "text": "微调训练完成后，用任意文本进行朗读测试。这是声音模型的全新能力。",
        "title": "微调模型验证",
    })
    s = wait_job(t["job_id"], timeout=900)
    check("微调模型合成", s["status"] == "success", f"({s.get('message')})")
    if s["status"] == "success":
        p = os.path.join(r"E:\go-project\src\audio-clone\data", s["result"]["path"].replace("/", "\\"))
        check("合成音频文件", os.path.exists(p) and os.path.getsize(p) > 10000, f"({os.path.getsize(p)} bytes)")

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")


if __name__ == "__main__":
    main()
