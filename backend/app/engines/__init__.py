"""引擎注册表。"""
from .base import BaseEngine, all_engines, get_engine, register
from . import edge_engine, fish_engine, gpt_sovits_engine, minimax_engine, openvino_engine

for _e in (
    edge_engine._engine,
    gpt_sovits_engine._engine,
    minimax_engine._engine,
    fish_engine._engine,
    openvino_engine._engine,
):
    register(_e)

__all__ = ["BaseEngine", "all_engines", "get_engine", "register"]
