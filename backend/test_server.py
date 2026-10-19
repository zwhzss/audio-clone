import httpx

c = httpx.Client(base_url="http://127.0.0.1:9880", timeout=60)
# 用最简单的参数测试
r = c.post("/tts", json={
    "text": "春江潮水连海平，海上明月共潮生。",
    "text_lang": "zh",
    "ref_audio_path": "E:\\go-project\\src\\audio-clone\\data\\cache\\ref_test.wav",
    "prompt_text": "小荷才露尖尖角，早有蜻蜓立上头。",
    "prompt_lang": "zh",
    "top_k": 15, "top_p": 0.85, "temperature": 0.72,
    "text_split_method": "cut3", "batch_size": 1,
    "speed_factor": 1.0, "stream": False, "media_type": "wav",
}, timeout=300)
print(f"status: {r.status_code}")
print(f"response: {r.text[:500]}")
