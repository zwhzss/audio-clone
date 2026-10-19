"""LLM 文稿润色：OpenAI 兼容 API（默认 DeepSeek，性价比最高）。

价格（元/百万 token，可在设置中调整）：
  deepseek-chat: 输入 ¥2（缓存未命中）/ 输出 ¥8
"""
from __future__ import annotations

import json

import httpx

from ..models import get_active_model, get_setting, get_setting_json

DEFAULT_PRICES = {
    "deepseek-v4-flash": {"input": 4.0, "output": 16.0},
    "deepseek-v4-pro": {"input": 6.0, "output": 27.0},
    "deepseek-chat": {"input": 4.0, "output": 16.0},   # 旧别名兼容
    "qwen-plus": {"input": 0.8, "output": 2.0},
    "glm-4-flash": {"input": 0.0, "output": 0.0},
}

STYLE_PRESETS = {
    "朗诵": "适合朗读的文本：语句通顺、节奏分明、适当留白，保留标点，不改变原意，去掉对朗读不利的书面语，输出纯文本。",
    "有声书": "适合有声书演播的文本：自然口语化、生动形象，避免长难句，每句适合一口气读完，输出纯文本。",
    "视频配音": "适合视频配音的文本：短句为主、节奏明快、口语化，适合 5~8 秒一个气口，输出纯文本。",
    "不改写": "原样输出文本，仅纠正明显的错别字与标点。",
}


def _get_llm_config(db) -> dict:
    """从模型库读取激活的润色模型（含 settings 兼容回退）。"""
    m = get_active_model(db, "llm")
    if m is not None:
        return {
            "api_key": m.api_key,
            "base_url": m.base_url or "https://api.deepseek.com/v1",
            "model": m.model or "deepseek-chat",
            "price_in": m.price or 0.0,
            "price_out": m.price_output or 0.0,
        }
    # 兼容回退：旧 settings
    return {
        "api_key": get_setting(db, "llm_api_key", ""),
        "base_url": get_setting(db, "llm_base_url", "https://api.deepseek.com/v1"),
        "model": get_setting(db, "llm_model", "deepseek-v4-flash"),
        "price_in": None,
        "price_out": None,
    }


def _compute_cost(model: str, in_tokens: int, out_tokens: int, prices: dict) -> float:
    p = prices.get(model, prices.get("deepseek-v4-flash", {"input": 4.0, "output": 16.0}))
    return round(in_tokens / 1e6 * float(p.get("input", 0)) + out_tokens / 1e6 * float(p.get("output", 0)), 6)


def polish_text(db, content: str, style: str = "朗诵", max_chars: int = 8000) -> dict:
    """润色文稿，返回 {result, cost, tokens}。"""
    cfg = _get_llm_config(db)
    if not cfg["api_key"]:
        active = get_active_model(db, "llm")
        if active is not None:
            raise RuntimeError(
                f"已激活润色模型「{active.name}」但未配置 API Key，请先在「模型库」补充密钥"
            )
        raise RuntimeError("未激活任何润色模型（模型库），请先添加并激活一个 LLM 模型")

    style_guide = STYLE_PRESETS.get(style, STYLE_PRESETS["朗诵"])
    if len(content) > max_chars:
        raise RuntimeError(f"文稿过长（{len(content)} 字符），单次润色上限 {max_chars} 字符，请分段")

    system = (
        "你是一名专业的有声读物/配音文稿编辑。请按要求改写用户提供的文本，"
        "保留原意、人名、数字与专有名词，不增删内容要点。"
        f"改写要求：{style_guide}"
    )
    resp = httpx.post(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0.7,
            "max_tokens": 8192,
        },
        timeout=300,
    )
    if resp.status_code != 200:
        try:
            err = resp.json()
            raise RuntimeError(f"LLM 错误: {err.get('error', {}).get('message', resp.text[:300])}")
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"LLM 错误 (HTTP {resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    result = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    in_tokens = int(usage.get("prompt_tokens", 0))
    out_tokens = int(usage.get("completion_tokens", 0))
    if cfg.get("price_in") is not None:
        cost = round(in_tokens / 1e6 * cfg["price_in"] + out_tokens / 1e6 * cfg["price_out"], 6)
    else:
        prices = get_setting_json(db, "llm_prices", DEFAULT_PRICES)
        cost = _compute_cost(cfg["model"], in_tokens, out_tokens, prices)
    return {"result": result, "cost": cost, "tokens": in_tokens + out_tokens}
