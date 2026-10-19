"""音频润色端到端验证：生成测试初稿 → 上传 → 参数渲染 → 质量检查（EQ/混响/压缩生效）。"""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0
FFMPEG = None
try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"


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


def wait_job(job_id, timeout=600):
    for _ in range(timeout // 2):
        time.sleep(2)
        s = req("GET", f"/api/jobs/{job_id}")
        if s["status"] not in ("queued", "running"):
            return s
    return {"status": "timeout"}


def main():
    # 1. 生成测试初稿（edge TTS 朗读）
    v = req("POST", "/api/voices", {"name": "润色测试", "source": "edge", "edge_voice": "zh-CN-YunxiNeural"})
    t = req("POST", "/api/synthesis/tts", {
        "voice_id": v["id"],
        "text": "这是我的朗诵初稿。在夜色中，我独自漫步在古老的街道上，感受着微风拂过脸庞的温柔。声音需要更加饱满，更有质感。",
        "title": "润色初稿",
    })
    s = wait_job(t["job_id"], timeout=300)
    check("生成测试初稿", s["status"] == "success")
    req("DELETE", f"/api/voices/{v['id']}")
    draft = os.path.join(r"E:\go-project\src\audio-clone\data", s["result"]["path"].replace("/", "\\"))

    # 2. 上传
    boundary = "----pl"
    with open(draft, "rb") as f:
        data = f.read()
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="draft.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    m = urllib.request.Request(BASE + "/api/polish/upload", data=b"".join(parts), method="POST")
    m.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(m, timeout=300) as resp:
        up = json.loads(resp.read().decode())
    check("上传初稿", up["duration"] > 5, f"({up['duration']}s)")

    # 3. 无参数渲染（应原样输出）
    r = req("POST", f"/api/polish/{up['task_dir']}/render", {"params": {}, "title": "默认"})
    s = wait_job(r["job_id"])
    check("默认渲染", s["status"] == "success")

    # 4. 满参数渲染（EQ+混响+压缩+降噪+响度）
    r = req("POST", f"/api/polish/{up['task_dir']}/render", {
        "params": {
            "eq": {"low": 4, "mid_low": 2, "mid": 1, "mid_high": 1, "high": 2},
            "highpass": 80, "lowpass": 15000,
            "reverb_amount": 30, "reverb_decay": 1.5,
            "compressor": {"enabled": True, "threshold": -22, "ratio": 3.5, "attack": 10, "release": 200, "makeup": 4},
            "noise_reduction": 40, "deesser": 30, "loudness": -16,
        },
        "title": "饱满润色",
    })
    s = wait_job(r["job_id"])
    check("满参数渲染", s["status"] == "success", f"({s.get('message')})")
    if s["status"] != "success":
        print("失败:", str(s.get("result", {}).get("traceback", ""))[-1000:])
        return
    out = os.path.join(r"E:\go-project\src\audio-clone\data", s["result"]["path"].replace("/", "\\"))
    check("输出文件有效", os.path.exists(out) and os.path.getsize(out) > 10000, f"({os.path.getsize(out)} bytes)")

    # 5. 质量检查：润色后响度应更高（压缩+makeup+EQ），低频能量增强
    import subprocess

    def analyze(path):
        p = subprocess.run([FFMPEG, "-hide_banner", "-i", path,
                            "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        mean = 0.0
        for line in p.stderr.splitlines():
            if "mean_volume" in line:
                mean = float(line.split(":")[-1].strip().replace(" dB", ""))
        return mean

    m_raw = analyze(draft)
    m_out = analyze(out)
    check("润色后响度提升（压缩+EQ+混响）", m_out > m_raw, f"(raw={m_raw:.1f}dB → out={m_out:.1f}dB)")

    # 6. 任务列表
    tl = req("GET", "/api/polish/tasks")
    check("任务列表", any(t["task_dir"] == up["task_dir"] and t["has_result"] for t in tl))

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")


if __name__ == "__main__":
    main()
