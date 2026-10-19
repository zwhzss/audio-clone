"""文稿路由：导入/编辑/LLM 润色（带费用统计）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engines.llm import polish_text
from ..jobs import enqueue
from ..models import Document
from ..text_utils import normalize_text

router = APIRouter(prefix="/api/documents", tags=["documents"])


class DocCreate(BaseModel):
    title: str = "未命名文稿"
    content: str = ""


class DocUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


class PolishBody(BaseModel):
    style: str = "朗诵"


@router.get("")
def list_documents(db: Session = Depends(get_db)):
    rows = db.execute(select(Document).order_by(Document.updated_at.desc())).scalars().all()
    return [d.to_dict() for d in rows]


@router.post("")
def create_document(body: DocCreate, db: Session = Depends(get_db)):
    doc = Document(title=body.title or "未命名文稿", content=normalize_text(body.content))
    db.add(doc)
    db.commit()
    return doc.to_dict()


@router.post("/upload")
async def upload_document(file: UploadFile, db: Session = Depends(get_db)):
    """上传 txt/md/srt 文稿文件。"""
    name = file.filename or "未命名文稿"
    raw = (await file.read()).decode("utf-8", errors="ignore")
    title = Path_stem(name)
    doc = Document(title=title, content=normalize_text(raw))
    db.add(doc)
    db.commit()
    return doc.to_dict()


def Path_stem(name: str) -> str:
    from pathlib import Path

    return Path(name).stem


@router.get("/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "文稿不存在")
    return d.to_dict()


@router.patch("/{doc_id}")
def update_document(doc_id: str, body: DocUpdate, db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "文稿不存在")
    if body.title is not None:
        d.title = body.title
    if body.content is not None:
        d.content = normalize_text(body.content)
    db.commit()
    return d.to_dict()


@router.post("/{doc_id}/polish")
def polish(doc_id: str, body: PolishBody, db: Session = Depends(get_db)):
    """LLM 润色（异步任务，记录 token 与费用）。"""
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "文稿不存在")
    if not d.content.strip():
        raise HTTPException(400, "文稿为空")

    def _run(update):
        from ..db import SessionLocal

        d2 = SessionLocal()
        try:
            doc = d2.get(Document, doc_id)
            if doc is None:
                raise RuntimeError("文稿已被删除")
            update(20, "调用 LLM 润色中…")
            r = polish_text(d2, doc.content, body.style)
            doc.polished = r["result"]
            doc.polish_style = body.style
            doc.llm_cost = round(doc.llm_cost + r["cost"], 6)
            doc.llm_tokens += r["tokens"]
            d2.commit()
            update(100, f"润色完成，花费 ¥{r['cost']:.6f}")
            return {"cost": r["cost"], "tokens": r["tokens"], "style": body.style}
        finally:
            d2.close()

    job_id = enqueue("polish", f"润色「{d.title}」", _run)
    return {"job_id": job_id}


@router.delete("/{doc_id}")
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "文稿不存在")
    db.delete(d)
    db.commit()
    return {"ok": True}
