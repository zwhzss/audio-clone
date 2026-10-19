"""歌曲分离端到端验证：合成测试歌曲（和弦伴奏+人声朗读）→ UVR5 分离 → 检查质量。"""
import json
import os
import subprocess
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


def wait_job(job_id, timeout=1800):
    for _ in range(timeout // 3):
        time.sleep(3)
        s = req("GET", f"/api/jobs/{job_id}")
        if s["status"] not in ("queued", "running"):
            return s
    return {"status": "timeout"}


def main():
    tmp = r"E:\go-project\src\audio-clone\data\cache\sep_test"
    os.makedirs(tmp, exist_ok=True)

    # 1. 合成测试歌曲：和弦伴奏（C 大三和弦循环）+ 人声（edge 朗读）
    bgm = os.path.join(tmp, "bgm.wav")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=261.63:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=329.63:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=392:duration=30",
                    "-filter_complex", "[0:a][1:a][2:a]amix=inputs=3:normalize=0,volume=0.35",
                    bgm], check=True)

    # 人声：edge TTS 朗读一段文字
    v = req("POST", "/api/voices", {"name": "sep测试", "source": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
    t = req("POST", "/api/synthesis/tts", {
        "voice_id": v["id"],
        "text": "这是一首测试歌曲。我们把语音和音乐混合在一起，用来验证人声与伴奏的分离效果。希望一切顺利。",
        "title": "分离测试人声",
    })
    s = wait_job(t["job_id"], timeout=300)
    check("生成人声轨", s["status"] == "success")
    req("DELETE", f"/api/voices/{v['id']}")
    vocal = os.path.join(r"E:\go-project\src\audio-clone\data", s["result"]["path"].replace("/", "\\"))

    # 混合成歌曲
    song = os.path.join(tmp, "test_song.wav")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", bgm, "-i", vocal,
                    "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0",
                    "-ac", "2", "-ar", "44100", song], check=True)
    check("合成测试歌曲", os.path.getsize(song) > 100000)

    # 2. 上传
    boundary = "----sep"
    with open(song, "rb") as f:
        data = f.read()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test_song.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    m = urllib.request.Request(BASE + "/api/separation/upload", data=b"".join(parts), method="POST")
    m.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(m, timeout=300) as resp:
        up = json.loads(resp.read().decode())
    check("上传歌曲", up["duration"] > 10, f"({up['duration']}s)")

    # 3. UVR5 分离
    r = req("POST", f"/api/separation/tasks/{up['task_dir']}/run",
            {"engine": "uvr5", "model": "HP2_all_vocals", "title": "测试分离"})
    s = wait_job(r["job_id"], timeout=1800)
    check("UVR5 分离完成", s["status"] == "success", f"({s.get('message')})")
    if s["status"] != "success":
        print("失败详情:", str(s.get("result", {}).get("traceback", ""))[-1000:])
        return
    result = s["result"]
    v_path = os.path.join(r"E:\go-project\src\audio-clone\data", result["vocals"].replace("/", "\\"))
    a_path = os.path.join(r"E:\go-project\src\audio-clone\data", result["accompaniment"].replace("/", "\\"))
    check("人声轨存在", os.path.exists(v_path) and os.path.getsize(v_path) > 10000, f"({os.path.getsize(v_path)}B)")
    check("伴奏轨存在", os.path.exists(a_path) and os.path.getsize(a_path) > 10000, f"({os.path.getsize(a_path)}B)")

    # 4. 质量检查：人声轨在语音段能量应显著高于纯伴奏段；伴奏轨反之
    import numpy as np

    def rms(path, start, end):
        p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                            "-i", path, "-f", "f32le", "-ac", "1", "-ar", "8000", "pipe:1"],
                           capture_output=True)
        if not p.stdout:
            return 0.0
        return float(np.sqrt(np.mean(np.frombuffer(p.stdout, dtype=np.float32) ** 2)))

    v_voice = rms(v_path, 1, 5)   # 人声朗读段（约 1-5 秒）
    v_tail = rms(v_path, 20, 24)  # 末尾纯伴奏段
    a_voice = rms(a_path, 1, 5)
    a_tail = rms(a_path, 20, 24)
    check("人声轨：语音段能量 > 伴奏段", v_voice > v_tail * 1.5, f"(voice={v_voice:.4f} vs tail={v_tail:.4f})")
    check("伴奏轨：全程有音乐能量", a_tail > 0.005, f"(tail rms={a_tail:.4f})")
    check("人声轨：伴奏段被压制", v_tail < a_tail * 0.9, f"(v_tail={v_tail:.4f} vs a_tail={a_tail:.4f})")

    # 5. 波形端点
    pk = req("GET", f"/api/audio/{result['vocals']}/peaks")
    check("人声波形", len(pk.get("peaks", [])) > 100)

    # 6. 任务列表
    tl = req("GET", "/api/separation/tasks")
    check("任务列表", any(t["task_dir"] == up["task_dir"] and t["has_result"] for t in tl))

    print(f"\n===== 结果：{PASS} 通过 / {FAIL} 失败 =====")


if __name__ == "__main__":
    main()
