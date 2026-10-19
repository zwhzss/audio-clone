"""GPT-SoVITS 端到端验证：创建声音 → 参考音频 → 克隆 → TTS 合成 → VC 音色转换。"""
import json
import sys
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
        return {"_raw": raw, "_ct": ct}


def wait_job(job_id, timeout=600):
    for _ in range(timeout // 3):
        time.sleep(3)
        s = req("GET", f"/api/jobs/{job_id}")
        if s["status"] not in ("queued", "running"):
            return s
    return {"status": "timeout"}


def upload_audio(voice_id, path):
    import urllib.request as ur

    boundary = "----ace2e"
    with open(path, "rb") as f:
        data = f.read()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"ref.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    m = ur.Request(BASE + f"/api/voices/{voice_id}/ref-audio", data=b"".join(parts), method="POST")
    m.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with ur.urlopen(m, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main():
    # 1. 生成测试参考音频（用 edge 合成一段中文，模拟用户录音）
    v_edge = req("POST", "/api/voices", {"name": "参考音频生成", "source": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
    tts = req("POST", "/api/synthesis/tts", {
        "voice_id": v_edge["id"],
        "text": "大家好，欢迎收听我的声音。这里是一段用于声音克隆的参考录音。",
        "title": "参考音频",
    })
    s = wait_job(tts["job_id"])
    check("生成参考音频", s["status"] == "success", f"({s['message']})")
    ref_path = s["result"]["path"]
    abs_ref = "E:/go-project/src/audio-clone/data/" + ref_path
    check("参考音频文件存在", __import__("os").path.exists(abs_ref.replace("/", "\\")))
    req("DELETE", f"/api/voices/{v_edge['id']}")

    # 2. 创建 GPT-SoVITS 声音 + 上传参考音频
    v = req("POST", "/api/voices", {
        "name": "GPT-SoVITS 测试音色", "source": "gpt_sovits",
        "ref_text": "大家好，欢迎收听我的声音。这里是一段用于声音克隆的参考录音。",
        "ref_lang": "zh", "timbre": "测试用",
    })
    up = upload_audio(v["id"], abs_ref.replace("/", "\\"))
    check("上传参考音频", up.get("duration", 0) > 3, f"({up.get('duration')}s)")

    # 3. 克隆（复制 ref 到服务端目录）
    c = req("POST", f"/api/voices/{v['id']}/clone")
    job_id = c.get("job_id")
    s = wait_job(job_id) if job_id else {"status": "none"}
    check("克隆完成", s["status"] == "success", f"({s.get('message', 'edge无任务')})")

    # 4. TTS 合成（克隆音色）
    t = req("POST", "/api/synthesis/tts", {
        "voice_id": v["id"], "text": "用克隆出来的声音，朗读一段测试文本。这是有声读物工作坊的验证。", "title": "克隆TTS验证",
    })
    s = wait_job(t["job_id"])
    check("克隆音色 TTS 合成", s["status"] == "success", f"({s.get('message')})")
    if s["status"] == "success":
        out = "E:/go-project/src/audio-clone/data/" + s["result"]["path"]
        import os

        check("TTS 音频文件", os.path.exists(out.replace("/", "\\")) and os.path.getsize(out.replace("/", "\\")) > 10000,
              f"({os.path.getsize(out.replace('/','\\\\')) if os.path.exists(out.replace('/','\\\\')) else 0} bytes)")

    # 5. 项目生成（gpt_sovits 分支：克隆音色直接合成；新版 api_v2 无 /vc 端点）
    doc = req("POST", "/api/documents", {"title": "gs测试", "content": "这是第一段测试文本。这是第二段测试文本。"})
    p = req("POST", "/api/projects", {"title": "gs作品", "document_id": doc["id"], "voice_id": v["id"]})
    g = req("POST", f"/api/projects/{p['id']}/generate")
    s = wait_job(g["job_id"], timeout=900)
    check("项目生成（克隆音色合成）", s["status"] == "success", f"({s.get('message')})")
    if s["status"] == "success":
        detail = req("GET", f"/api/projects/{p['id']}")
        check("项目声音轨/成品路径", bool(detail["voice_track_path"]) and len(detail["segments"]) >= 1,
              f"({len(detail['segments'])} 段)")

    # 清理
    req("DELETE", f"/api/projects/{p['id']}")
    req("DELETE", f"/api/documents/{doc['id']}")
    req("DELETE", f"/api/voices/{v['id']}")

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
