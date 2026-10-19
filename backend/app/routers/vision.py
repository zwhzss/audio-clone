"""图片/文字 → 语音：视觉 LLM 描述图片 → TTS 生成音频。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .config import get_setting


def describe_image(db, image_path: str, style: str = "朗读") -> str:
    """用视觉 LLM 描述图片内容，返回文字描述。"""
    import base64
    import mimetypes

    mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    m = get_active_model(db, "llm")
    if m is None:
        raise RuntimeError("未激活润色模型（模型库），视觉描述需要一个多模态 LLM（如 DeepSeek-VL/Qwen-VL），请在模型库添加并激活")
    key = m.api_key
    base = m.base_url or "https://api.deepseek.com/v1"
    model = m.model or "deepseek-v4-flash"
    if not key:
        raise RuntimeError("润色模型未配置 API Key（模型库）")

    style_prompt = {
        "朗读": "请用自然流畅的中文详细描述这张图片的内容，适合直接朗读给听众听。",
        "有声书": "请用生动有画面感的中文描述这张图片，像在讲故事一样，让听众能感受到场景。",
        "短解说": "请用简洁生动的中文一句话或几句话概括这张图片的核心内容，适合视频配音。",
        "诗意": "请用诗意优美的中文描述这张图片的意境，像一首小诗。",
    }.get(style, style_prompt.get("朗读", "请描述这张图片。"))

    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                {"type": "text", "text": style_prompt},
            ]},
        ],
        "max_tokens": 500,
    }
    resp = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=120,
    )
    if resp.status_code != 200:
        err = resp.json().get("error", {}).get("message", resp.text[:200])
        raise RuntimeError(f"视觉模型请求失败 ({resp.status_code}): {err}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    in_t = int(usage.get("prompt_tokens", 0))
    out_t = int(usage.get("completion_tokens", 0))
    return {"text": text, "tokens": in_t + out_t, "cost": 0.0, "model": model}
