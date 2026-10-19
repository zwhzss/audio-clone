"""SQLite 数据库：引擎与会话。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_URL

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401 确保模型注册
    from .digital_human import DigitalHuman  # noqa: F401
    from .training_record import TrainingRecord  # noqa: F401
    from .training_slice import TrainingSlice  # noqa: F401
    from .slicing import AudioSlice, SliceTask  # noqa: F401  确保分片模块的表被建出来

    Base.metadata.create_all(engine)
    _migrate()
    _seed_default_models()


def _seed_default_models():
    """把旧 settings 中的密钥/价格迁移为缺省模型条目（仅首次）。"""
    from sqlalchemy import select, text

    from .models import AIModel, Setting, get_setting

    with engine.begin() as conn:
        cnt = conn.execute(text("SELECT COUNT(*) FROM ai_models")).scalar()
        if cnt and cnt > 0:
            return

        def _has(key: str) -> bool:
            return conn.execute(text("SELECT COUNT(*) FROM settings WHERE key=:k"), {"k": key}).scalar() > 0

        def _get(key: str, default: str = "") -> str:
            row = conn.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
            return row[0] if row else default

        # 旧 deepseek-chat 已更名为 deepseek-v4-flash
        _old_llm = _get("llm_model", "")
        _llm_model = _old_llm if _old_llm and "v4" in _old_llm else "deepseek-v4-flash"

        presets = [
            # 语音类（voice）
            dict(kind="voice", provider="edge", name="Edge 免费音色", model="edge",
                 api_key="", base_url="", price=0.0, price_output=0.0, price_unit="免费",
                 desc="微软神经语音，联网即可、无需密钥；适合初版音频与日常合成",
                 extra={}, is_preset=1, is_active=1),
            dict(kind="voice", provider="openvino", name="OpenVINO（NPU 端侧）", model="openvino",
                 api_key="", base_url="", price=0.0, price_output=0.0, price_unit="免费",
                 desc="本地 NPU/CPU 推理，免联网免费用；需在 extra.model_dir 配置模型目录",
                 extra={"model_dir": _get("openvino_model_dir", ""), "device": _get("openvino_device", "NPU")},
                 is_preset=1, is_active=0),
            dict(kind="voice", provider="minimax", name="MiniMax speech-02", model=_get("minimax_model", "speech-02-hd"),
                 api_key=_get("minimax_api_key", ""),
                 base_url="https://api.minimaxi.com",
                 price=float(_get("minimax_price_per_10k", "4") or 4), price_output=0.0, price_unit="万字符",
                 desc="请求时复刻音色；中文音质好，无需本地 GPU",
                 extra={"group_id": _get("minimax_group_id", "")},
                 is_preset=1, is_active=1 if _get("minimax_api_key", "") else 0),
            dict(kind="voice", provider="fish", name="Fish Audio", model="fish-tts",
                 api_key=_get("fish_api_key", ""),
                 base_url="https://api.fish.audio",
                 price=float(_get("fish_price_per_10k", "5") or 5), price_output=0.0, price_unit="万字符",
                 desc="积分制；支持平台训练专属音色",
                 extra={}, is_preset=1, is_active=1 if _get("fish_api_key", "") else 0),
            # 润色类（llm）
            dict(kind="llm", provider="deepseek", name="DeepSeek V4-Flash", model=_llm_model,
                 api_key=_get("llm_api_key", ""),
                 base_url=_get("llm_base_url", "https://api.deepseek.com/v1"),
                 price=4.0, price_output=16.0, price_unit="百万token",
                 desc="官方最新模型（deepseek-chat 已更名）；峰谷定价，空闲时段半价",
                 extra={}, is_preset=1, is_active=1 if _get("llm_api_key", "") else 0),
        ]
        for p in presets:
            conn.execute(
                text("""INSERT INTO ai_models
                    (id, kind, provider, name, model, api_key, base_url, price, price_output,
                     price_unit, desc, extra, is_active, is_preset, created_at)
                    VALUES (:id, :kind, :provider, :name, :model, :api_key, :base_url, :price, :price_output,
                     :price_unit, :desc, :extra, :is_active, :is_preset, datetime('now'))"""),
                {**p, "id": __import__("uuid").uuid4().hex[:12], "extra": __import__("json").dumps(p["extra"])},
            )


def _migrate():
    """轻量迁移：为已有表补充新增列（SQLite ALTER TABLE ADD COLUMN）。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    from .models import Project, Segment

    adds = {
        Segment.__tablename__: ["params", "offset"],
        Project.__tablename__: ["chapters", "digital_human_id", "ref_audio_path", "ref_text"],
        "digital_humans": ["s1_data_dir", "s2_data_dir"],
        "training_slices": ["slice_task_id"],
        "audio_slices": ["flags"],
    }
    with engine.begin() as conn:
        for table, cols in adds.items():
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col in cols:
                if col not in existing:
                    ddl = {
                        "params": "JSON DEFAULT '{}'",
                        "offset": "FLOAT DEFAULT 0.0",
                        "chapters": "JSON DEFAULT '[]'",
                        "digital_human_id": "VARCHAR(16) DEFAULT ''",
                        "ref_audio_path": "VARCHAR(500) DEFAULT ''",
                        "ref_text": "TEXT DEFAULT ''",
                        "s1_data_dir": "VARCHAR(500) DEFAULT ''",
                        "s2_data_dir": "VARCHAR(500) DEFAULT ''",
                        "slice_task_id": "VARCHAR(16) DEFAULT ''",
                        "flags": "VARCHAR(60) DEFAULT ''",
                    }[col]
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
