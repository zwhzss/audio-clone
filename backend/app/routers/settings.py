"""设置路由：API 密钥、价格表、引擎配置 + 任务查询。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engines.llm import DEFAULT_PRICES
from ..jobs import get_job
from ..models import Job, get_setting, get_setting_json, set_setting, set_setting_json

router = APIRouter(prefix="/api", tags=["settings"])

# 敏感键：展示时打码
_SECRET_KEYS = {"minimax_api_key", "fish_api_key", "llm_api_key"}

SETTING_KEYS = [
    "minimax_api_key", "minimax_group_id", "minimax_model", "minimax_price_per_10k",
    "fish_api_key", "fish_price_per_10k",
    "llm_api_key", "llm_base_url", "llm_model",
    "openvino_model_dir", "openvino_device", "openvino_max_audio",
    "asr_verify_mode",   # full | smart | off
]


class SettingsBody(BaseModel):
    values: dict


def _mask(key: str, value: str) -> str:
    if key in _SECRET_KEYS and value:
        if len(value) <= 8:
            return "*" * len(value)
        return value[:4] + "****" + value[-4:]
    return value


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    out = {}
    for k in SETTING_KEYS:
        out[k] = _mask(k, get_setting(db, k, ""))
    out["llm_prices"] = get_setting_json(db, "llm_prices", DEFAULT_PRICES)
    return out


@router.post("/settings")
def save_settings(body: SettingsBody, db: Session = Depends(get_db)):
    for k, v in body.values.items():
        if k in SETTING_KEYS:
            # 打码值不改动原密钥
            if k in _SECRET_KEYS and isinstance(v, str) and "*" in v:
                continue
            set_setting(db, k, str(v))
        elif k == "llm_prices" and isinstance(v, dict):
            set_setting_json(db, k, v)
    db.commit()
    return {"ok": True}


@router.post("/settings/test-llm")
def test_llm(db: Session = Depends(get_db)):
    """测试 LLM 连通性。"""
    key = get_setting(db, "llm_api_key", "")
    base = get_setting(db, "llm_base_url", "https://api.deepseek.com/v1")
    model = get_setting(db, "llm_model", "deepseek-chat")
    if not key:
        raise HTTPException(400, "未配置 LLM API Key")
    try:
        r = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
            timeout=30,
        )
        if r.status_code == 200:
            return {"ok": True, "message": f"连接成功（{model}）"}
        return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@router.get("/jobs")
def recent_jobs(db: Session = Depends(get_db), limit: int = 20):
    rows = db.execute(select(Job).order_by(Job.created_at.desc()).limit(min(limit, 100))).scalars().all()
    return [j.to_dict() for j in rows]


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    """取消/终止运行中的 job（设置状态为 error）。"""
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    if j.status not in ("running", "queued"):
        return {"ok": True, "message": "任务已完成或已取消"}
    from ..jobs import request_cancel

    request_cancel(job_id)          # 让正在跑的线程在下一个检查点主动停下
    j.status = "error"
    j.message = "用户取消"
    from ..db import SessionLocal
    d = SessionLocal()
    try:
        job_db = d.get(Job, job_id)
        if job_db:
            job_db.status = "error"
            job_db.message = "用户取消"
            d.commit()
    finally:
        d.close()
    return {"ok": True, "message": "已取消"}
