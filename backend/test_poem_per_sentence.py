import json
import os
import subprocess
import urllib.request
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BASE_API = "http://127.0.0.1:9880"
APP_API = "http://127.0.0.1:8000"
CACHE = r"E:\go-project\src\audio-clone\data\cache"

d = json.loads(urllib.request.urlopen(APP_API + "/api/humans/312deb395270").read().decode())
vid = d["voice_id"]
v = json.loads(urllib.request.urlopen(APP_API + f"/api/voices/{vid}").read().decode())
ec = v["engine_config"]

POEM = "天门中断楚江开，碧水东流至此回。两岸青山相对出，孤帆一片日边来。"
sentences = [s.strip() + "。" for s in POEM.split("。") if s.strip()]
print(f"诗句分割: {sentences}")

parts = []
for i, sent in enumerate(sentences):
    print(f"合成第 {i+1} 句: {sent}")
    data = {
        "text": sent, "text_lang": "zh",
        "ref_audio_path": ec.get("ref_path", ""),
        "prompt_text": ec.get("ref_text", ""),
        "prompt_lang": "zh", "top_k": 15, "top_p": 0.85,
        "temperature": 0.72, "text_split_method": "cut3",
        "batch_size": 1, "speed_factor": 1.0,
        "stream": False, "media_type": "wav",
    }
    body = json.dumps(data, ensure_ascii=False).encode()
    r = urllib.request.Request(f"{BASE_API}/tts", data=body, method="POST",
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=300) as resp:
        audio = resp.read()
    part_path = os.path.join(CACHE, f"poem_sent_{i}.wav")
    with open(part_path, "wb") as f:
        f.write(audio)
    parts.append(part_path)
    print(f"  第 {i+1} 句: {len(audio)} bytes")

list_file = os.path.join(CACHE, "poem_concat.txt")
with open(list_file, "w", encoding="utf-8") as f:
    for p in parts:
        f.write(f"file '{p}'\n")

out = os.path.join(CACHE, "poem_concat.wav")
subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out], capture_output=True)
print(f"\n拼接完成: {out}, {os.path.getsize(out)} bytes")
