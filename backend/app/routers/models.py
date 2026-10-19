"""模型库路由：商用模型配置管理（增删改查/激活切换/预设模板/连通性测试）。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AIModel, Voice, get_active_model

router = APIRouter(prefix="/api/models", tags=["models"])

# ---------------- 预设模板（可一键追加，价格均为参考价可编辑） ----------------

PRESET_TEMPLATES = [
    # 语音类
    {"kind": "voice", "provider": "edge", "name": "Edge 免费音色", "model": "edge",
     "base_url": "", "price": 0, "price_output": 0, "price_unit": "免费",
     "desc": "微软神经语音，联网即可、无需密钥；适合初版音频与日常合成", "extra": {}},
    {"kind": "voice", "provider": "openvino", "name": "OpenVINO（NPU 端侧）", "model": "openvino",
     "base_url": "", "price": 0, "price_output": 0, "price_unit": "免费",
     "desc": "本地 NPU/CPU 推理，免联网免费用；需在「扩展配置」填模型目录", "extra": {"model_dir": "", "device": "NPU"}},
    {"kind": "voice", "provider": "minimax", "name": "MiniMax speech-02", "model": "speech-02",
     "base_url": "https://api.minimaxi.com", "price": 3, "price_output": 0, "price_unit": "万字符",
     "desc": "请求时复刻音色；中文音质好，性价比高", "extra": {"group_id": ""}},
    {"kind": "voice", "provider": "minimax", "name": "MiniMax speech-02-hd", "model": "speech-02-hd",
     "base_url": "https://api.minimaxi.com", "price": 4, "price_output": 0, "price_unit": "万字符",
     "desc": "高清音质，适合有声书成品", "extra": {"group_id": ""}},
    {"kind": "voice", "provider": "fish", "name": "Fish Audio", "model": "fish-tts",
     "base_url": "https://api.fish.audio", "price": 5, "price_output": 0, "price_unit": "万字符",
     "desc": "积分制；支持平台训练专属音色", "extra": {}},
    {"kind": "voice", "provider": "volcengine", "name": "火山引擎语音合成", "model": "大模型语音合成",
     "base_url": "https://openspeech.bytedance.com/api/v3", "price": 2, "price_output": 0, "price_unit": "万字符",
     "desc": "豆包语音音色丰富，需应用 ID/访问令牌", "extra": {}},
    {"kind": "voice", "provider": "azure", "name": "Azure 神经语音", "model": "zh-CN-XiaoxiaoNeural",
     "base_url": "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1", "price": 6, "price_output": 0, "price_unit": "万字符",
     "desc": "微软云，需 Speech Key 与 Region", "extra": {"region": "eastasia"}},
    # 润色类（LLM）
    {"kind": "llm", "provider": "deepseek", "name": "DeepSeek V4-Flash", "model": "deepseek-v4-flash",
     "base_url": "https://api.deepseek.com/v1", "price": 4, "price_output": 16, "price_unit": "百万token",
     "desc": "官方最新模型（deepseek-chat 已更名）；峰谷定价，高峰 9-12/14-18 点，空闲半价", "extra": {}},
    {"kind": "llm", "provider": "deepseek", "name": "DeepSeek V4-Pro", "model": "deepseek-v4-pro",
     "base_url": "https://api.deepseek.com/v1", "price": 6, "price_output": 27, "price_unit": "百万token",
     "desc": "正式版，Agent/推理能力强；峰谷定价，参考价可编辑", "extra": {}},
    {"kind": "llm", "provider": "qwen", "name": "通义千问 Qwen-Plus", "model": "qwen-plus",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "price": 0.8, "price_output": 2, "price_unit": "百万token",
     "desc": "阿里云，比 DeepSeek 更便宜", "extra": {}},
    {"kind": "llm", "provider": "qwen", "name": "通义千问 Qwen-Turbo", "model": "qwen-turbo",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "price": 0.3, "price_output": 0.6, "price_unit": "百万token",
     "desc": "极速极便宜，润色够用", "extra": {}},
    {"kind": "llm", "provider": "glm", "name": "智谱 GLM-4-Flash", "model": "glm-4-flash",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "price": 0, "price_output": 0, "price_unit": "百万token",
     "desc": "完全免费！适合低成本批量润色", "extra": {}},
    {"kind": "llm", "provider": "kimi", "name": "Moonshot Kimi", "model": "moonshot-v1-8k",
     "base_url": "https://api.moonshot.cn/v1", "price": 12, "price_output": 12, "price_unit": "百万token",
     "desc": "长文本友好", "extra": {}},
    {"kind": "llm", "provider": "doubao", "name": "火山方舟豆包", "model": "doubao-seed-1.6",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "price": 1, "price_output": 8, "price_unit": "百万token",
     "desc": "字节跳动，中文性价比好", "extra": {}},
    {"kind": "llm", "provider": "openai", "name": "OpenAI GPT-4o-mini", "model": "gpt-4o-mini",
     "base_url": "https://api.openai.com/v1", "price": 1.1, "price_output": 4.4, "price_unit": "百万token",
     "desc": "国际通用（元计价，需海外网络）", "extra": {}},
]


class ModelCreate(BaseModel):
    kind: str = "voice"
    provider: str = "custom"
    name: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    price: float = 0.0
    price_output: float = 0.0
    price_unit: str = "万字符"
    desc: str = ""
    extra: dict = {}
    activate: bool = True   # 创建后是否直接激活


class ModelUpdate(BaseModel):
    name: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    price: float | None = None
    price_output: float | None = None
    price_unit: str | None = None
    desc: str | None = None
    extra: dict | None = None


def _mask_key(v: str) -> str:
    if not v:
        return ""
    return (v[:4] + "****" + v[-4:]) if len(v) > 8 else "*" * len(v)


@router.get("")
def list_models(db: Session = Depends(get_db), kind: str = ""):
    q = select(AIModel).order_by(AIModel.kind, AIModel.is_active.desc(), AIModel.created_at)
    if kind:
        q = q.where(AIModel.kind == kind)
    rows = db.execute(q).scalars().all()
    return [m.to_dict() for m in rows]


@router.get("/presets")
def preset_templates():
    return PRESET_TEMPLATES


@router.post("")
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    if body.kind not in ("voice", "llm"):
        raise HTTPException(400, "kind 必须为 voice 或 llm")
    if not body.name.strip():
        raise HTTPException(400, "请填写模型名称")
    m = AIModel(
        kind=body.kind, provider=body.provider or "custom",
        name=body.name.strip(), model=body.model, api_key=body.api_key or "",
        base_url=body.base_url or "", price=body.price or 0.0,
        price_output=body.price_output or 0.0, price_unit=body.price_unit or "万字符",
        desc=body.desc, extra=body.extra or {},
        is_active=1 if body.activate else 0, is_preset=0,
    )
    db.add(m)
    if body.activate:
        _deactivate_others(db, body.kind, None)
    db.commit()
    return m.to_dict()


@router.patch("/{model_id}")
def update_model(model_id: str, body: ModelUpdate, db: Session = Depends(get_db)):
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "api_key" and isinstance(v, str) and "*" in v:
            continue  # 打码值不改动原密钥
        setattr(m, k, v)
    db.commit()
    return m.to_dict()


@router.post("/{model_id}/activate")
def activate_model(model_id: str, db: Session = Depends(get_db)):
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    _deactivate_others(db, m.kind, model_id)
    m.is_active = 1
    db.commit()
    return m.to_dict()


@router.post("/{model_id}/deactivate")
def deactivate_model(model_id: str, db: Session = Depends(get_db)):
    """停用该模型（同类别的其他模型不受影响；voice 类停用后自动回退 Edge 免费音色）。"""
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    m.is_active = 0
    db.commit()
    return m.to_dict()


def _deactivate_others(db: Session, kind: str, keep_id: str | None):
    rows = db.execute(select(AIModel).where(AIModel.kind == kind)).scalars().all()
    for r in rows:
        if r.id != keep_id:
            r.is_active = 0


@router.delete("/{model_id}")
def delete_model(model_id: str, db: Session = Depends(get_db)):
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    # 不允许删除同类的最后一个激活模型，保证 voice 类始终有可用引擎
    if m.is_active:
        others = db.execute(
            select(AIModel).where(AIModel.kind == m.kind, AIModel.id != m.id)
        ).scalars().all()
        if not others:
            raise HTTPException(400, "该类别没有其他模型了，请先追加再删除")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/{model_id}/test")
def test_model(model_id: str, db: Session = Depends(get_db)):
    """连通性测试：LLM 发真实请求；语音类检查密钥/模型配置（不产生计费）。"""
    m = db.get(AIModel, model_id)
    if not m:
        raise HTTPException(404, "模型不存在")
    if m.kind == "llm":
        if not m.api_key:
            return {"ok": False, "message": "未填写 API Key"}
        try:
            r = httpx.post(
                f"{m.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {m.api_key}"},
                json={"model": m.model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
                timeout=30,
            )
            if r.status_code == 200:
                return {"ok": True, "message": f"连接成功（{m.model}）"}
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
    # 语音类
    if m.provider in ("edge", "openvino"):
        return {"ok": True, "message": "本地引擎，无需测试（可用性见引擎状态）"}
    if m.provider == "minimax":
        gid = (m.extra or {}).get("group_id", "")
        if not m.api_key or not gid:
            return {"ok": False, "message": "缺少 API Key 或 GroupId"}
        return {"ok": True, "message": "配置完整（实际合成即验证）"}
    if m.provider == "fish":
        if not m.api_key:
            return {"ok": False, "message": "未填写 API Key"}
        return {"ok": True, "message": "配置完整（实际合成即验证）"}
    return {"ok": True, "message": "自定义平台：请用「快速合成」实际验证"}
