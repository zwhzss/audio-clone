"""任务队列：线程池 + GPU 串行锁，进度写回数据库供前端轮询。"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime
from typing import Callable

from .db import SessionLocal
from .models import Job

# 全局线程池（简单版：最多 N 个 worker）
_N_WORKERS = 2
_GPU_LOCK = threading.Lock()
_QUEUE: list[dict] = []
_QUEUE_CV = threading.Condition()
_STARTED = False


def _run_worker():
    while True:
        with _QUEUE_CV:
            while not _QUEUE:
                _QUEUE_CV.wait()
            item = _QUEUE.pop(0)
            gpu = item.get("gpu", False)

        job_id, fn = item["job_id"], item["fn"]
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is None:
                continue
            job.status = "running"
            job.message = "开始执行…"
            db.commit()
        finally:
            db.close()

        def update(progress: int, message: str = ""):
            d = SessionLocal()
            try:
                j = d.get(Job, job_id)
                if j is None:
                    return
                if progress is not None:
                    j.progress = max(0, min(100, int(progress)))
                if message:
                    j.message = message
                d.commit()
            finally:
                d.close()

        try:
            if gpu:
                with _GPU_LOCK:
                    result = fn(update)
            else:
                result = fn(update)
            d = SessionLocal()
            try:
                j = d.get(Job, job_id)
                if j is not None:
                    if is_cancelled(job_id):
                        # 用户已点取消：别让收尾把它复活成 success
                        j.status = "error"
                        j.message = "用户取消"
                    else:
                        j.status = "success"
                        j.progress = 100
                        j.message = "完成"
                        j.result = result or {}
                    j.finished_at = datetime.now()
                    d.commit()
            finally:
                d.close()
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            d = SessionLocal()
            try:
                j = d.get(Job, job_id)
                if j is not None:
                    j.status = "error"
                    j.message = f"{e}"
                    j.result = {"traceback": tb[-4000:]}
                    j.finished_at = datetime.now()
                    d.commit()
            finally:
                d.close()
        finally:
            clear_cancel(job_id)


def enqueue(kind: str, title: str, fn: Callable, gpu: bool = False) -> str:
    """提交一个后台任务，返回 job_id。fn 签名: fn(update) -> dict。"""
    global _STARTED
    with _QUEUE_CV:
        if not _STARTED:
            _STARTED = True
            for _ in range(_N_WORKERS):
                threading.Thread(target=_run_worker, daemon=True).start()

    db = SessionLocal()
    try:
        job = Job(kind=kind, title=title, status="queued", message="排队中…")
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    with _QUEUE_CV:
        _QUEUE.append({"job_id": job_id, "fn": fn, "gpu": gpu})
        _QUEUE_CV.notify_all()
    return job_id


def get_job(job_id: str) -> dict | None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        return job.to_dict() if job else None
    finally:
        db.close()


_CANCELLED: set[str] = set()


def request_cancel(job_id: str) -> None:
    """标记任务为「已请求取消」。正在跑的线程会在下一个检查点主动停下。"""
    with _QUEUE_CV:
        _CANCELLED.add(job_id)


def is_cancelled(job_id: str) -> bool:
    return job_id in _CANCELLED


def clear_cancel(job_id: str) -> None:
    with _QUEUE_CV:
        _CANCELLED.discard(job_id)


def sweep_interrupted() -> int:
    """启动时把上个进程遗留的任务与项目状态标记为中断。

    任务由本进程的线程池执行，进程一死就再没人更新它 —— 不扫的话界面上会留下
    永远转圈的假任务（实测后端重启后 project_generate 停在 55% 不动，
    项目也一直卡在 generating）。
    """
    from .models import Project

    n = 0
    db = SessionLocal()
    try:
        now = datetime.now()
        for j in db.query(Job).filter(Job.status.in_(("running", "queued"))).all():
            j.status = "error"
            j.finished_at = now
            j.message = "服务重启，任务被中断（不是合成失败）。请重新发起。"
            n += 1
        for p in db.query(Project).filter(Project.status.in_(("generating", "rendering"))).all():
            p.status = "error"
            p.error = "服务重启，声音轨/渲染被中断，请重新发起"
            n += 1
        db.commit()
    finally:
        db.close()
    return n
