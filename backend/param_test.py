"""参数对比测试：不同参数组合合成同一文本，找出最佳配置。"""
import json, os, subprocess
import httpx
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
c0 = httpx.Client(timeout=30)
d = json.loads(c0.get("http://127.0.0.1:8000/api/humans/312deb395270").text)
vid = d["voice_id"]
v = json.loads(c0.get(f"http://127.0.0.1:8000/api/voices/{vid}").text)
ec = v["engine_config"]
c = httpx.Client(base_url="http://127.0.0.1:9880", timeout=60)
c.get(f"/set_gpt_weights?weights_path={ec['gpt_model']}")
c.get(f"/set_sovits_weights?weights_path={ec['sovits_model']}")

CACHE = r"E:\go-project\src\audio-clone\data\cache"
test = "天门中断楚江开。"

# 不同参数组合
configs = [
    {"name": "默认参数", "temp": 0.72, "top_p": 0.85, "top_k": 15, "split": "cut3"},
    {"name": "高温度", "temp": 1.0, "top_p": 0.9, "top_k": 20, "split": "cut3"},
    {"name": "低温度", "temp": 0.5, "top_p": 0.8, "top_k": 10, "split": "cut3"},
    {"name": "cut5", "temp": 0.72, "top_p": 0.85, "top_k": 15, "split": "cut5"},
]

for cfg in configs:
    print(f"\n测试: {cfg['name']}")
    r = c.post("/tts", json={
        "text": test, "text_lang": "zh",
        "ref_audio_path": ec.get("ref_path", ""),
        "prompt_text": ec.get("ref_text", ""),
        "prompt_lang": "zh",
        "top_k": cfg["top_k"], "top_p": cfg["top_p"],
        "temperature": cfg["temp"],
        "text_split_method": cfg["split"],
        "batch_size": 1, "speed_factor": 1.0,
        "stream": False, "media_type": "wav",
    }, timeout=300)
    p = os.path.join(CACHE, f"param_{cfg['name']}.wav")
    with open(p, "wb") as f:
        f.write(r.content)
    print(f"  {len(r.content)} bytes → {p}")

print("\n请播放对比:")
for cfg in configs:
    print(f"  {CACHE}\\param_{cfg['name']}.wav")
