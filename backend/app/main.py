"""FastAPI 入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import ROOT_DIR
from .db import init_db
from .routers import documents, humans, mix, models as models_router, polish, projects, separation, settings as settings_router, slicing, synthesis, voices

app = FastAPI(title="声音克隆 · 有声读物工坊", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

# 上个进程遗留的 running/queued 任务永远不会再被更新，启动时统一标为中断
from .jobs import sweep_interrupted  # noqa: E402

sweep_interrupted()

app.include_router(voices.router)
app.include_router(documents.router)
app.include_router(synthesis.router)
app.include_router(mix.router)
app.include_router(projects.router)
app.include_router(models_router.router)
app.include_router(separation.router)
app.include_router(slicing.router)
app.include_router(polish.router)
app.include_router(humans.router)
app.include_router(settings_router.router)


@app.get("/api/health")
def health():
    return {"ok": True, "app": "audio-clone", "ffmpeg": "ok"}


# 前端构建产物（frontend/dist）存在时挂载为静态站点
_dist = ROOT_DIR / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
