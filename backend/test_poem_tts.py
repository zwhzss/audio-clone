"""真实诗歌合成测试：直接调用 GPT-SoVITS API，验证输出质量。"""
import json
import sys
import time
import urllib.request
import subprocess
import imageio_ffmpeg

BASE_API = "http://127.0.0.1:9880"
APP_API = "http://127.0.0.1:8000"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# 测试诗歌
POEM = "天门中断楚江开，碧水东流至此回。两岸青山相对出，孤帆一片日边来。"

# 获取数字人绑定的声音配置
d = json.loads(urllib.request.urlopen(APP_API + "/api/humans/312deb395270").read().decode())
vid = d["voice_id"]
v = json.loads(urllib.request.urlopen(APP_API + f"/api/voices/{vid}").read().decode())
ec = v["engine_config"]
print("ref_path:", ec.get("ref_path", "")[:60])
print("gpt_model:", ec.get("gpt_model", "")[:60])
print("sovits_model:", ec.get("sovits_model", "")[:60])

# 测试1：直接发送原始文本，用 cut3
print("\n=== 测试1：原始文本 + cut3 ===")
data = {
    "text": POEM,
    "text_lang": "zh",
    "ref_audio_path": ec.get("ref_path", ""),
    "prompt_text": ec.get("ref_text", ""),
    "prompt_lang": "zh",
    "top_k": 15,
    "top_p": 0.85,
    "temperature": 0.72,
    "text_split_method": "cut3",
    "batch_size": 1,
    "speed_factor": 1.0,
    "stream": False,
    "media_type": "wav",
}
body = json.dumps(data, ensure_ascii=False).encode()
r = urllib.request.Request(f"{BASE_API}/tts", data=body, method="POST",
                           headers={"Content-Type": "application/json"})
with urllib.request.urlopen(r, timeout=300) as resp:
    audio = resp.read()
out1 = r"E:\go-project\src\audio-clone\data\cache\poem_test1.wav"
with open(out1, "wb") as f:
    f.write(audio)
print(f"输出: {len(audio)} bytes → {out1}")
# 转 wav 检查
wav1 = out1.replace(".wav", "_final.wav")
subprocess.run([FFMPEG, "-y", "-i", out1, "-ar", "24000", wav1], capture_output=True)
print(f"最终: {wav1}")

# 测试2：预处理后文本 + cut3
print("\n=== 测试2：预处理文本 + cut3 ===")
from app.text_utils import prepare_tts_text
processed = prepare_tts_text(POEM)
print(f"预处理后: {processed}")
data["text"] = processed
body = json.dumps(data, ensure_ascii=False).encode()
r = urllib.request.Request(f"{BASE_API}/tts", data=body, method="POST",
                           headers={"Content-Type": "application/json"})
with urllib.request.urlopen(r, timeout=300) as resp:
    audio = resp.read()
out2 = r"E:\go-project\src\audio-clone\data\cache\poem_test2.wav"
with open(out2, "wb") as f:
    f.write(audio)
wav2 = out2.replace(".wav", "_final.wav")
subprocess.run([FFMPEG, "-y", "-i", out2, "-ar", "24000", wav2], capture_output=True)
print(f"输出: {len(audio)} bytes → {wav2}")

print("\n=== 测试完成 ===")
print(f"测试1（原始文本）: {out1}")
print(f"测试2（预处理文本）: {out2}")
print("请对比两个文件的输出质量")
