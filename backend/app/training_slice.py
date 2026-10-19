"""训练切片表：数据库管理音频-文字对应关系（替代 train.list 文件管理）。"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String

from .db import Base
from .models import _uid


class TrainingSlice(Base):
    """训练数据的切片记录：每个音频切片 + 对应标注文字。"""

    __tablename__ = "training_slices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    human_id = Column(String(16), index=True)
    audio = Column(String(500), default="")          # 音频文件名（相对于训练目录）
    text = Column(String(2000), default="")           # 标注文字
    lang = Column(String(10), default="zh")
    category = Column(String(20), default="other")   # prose/poetry/fiction/classical/other
    is_deleted = Column(Integer, default=0)           # 软删除（0=正常，1=已删除）
    sort_order = Column(Integer, default=0)           # 排序序号
    data_dir = Column(String(500), default="")        # 数据目录（区分不同训练批次）
    slice_task_id = Column(String(16), default="")    # 由音频分片任务导入时记录来源任务 id
    flags = Column(String(60), default="")            # no_break/quote：识别对齐切分时的可疑标记
