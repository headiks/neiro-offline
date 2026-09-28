"""Этапы обучения и очередь вопросов без ответа (эскалация человеку)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import stages
import questions
import qacache
from deps import require_admin, admin_only

router = APIRouter()


# ---------- Этапы обучения (структура, к которой привязываются папки) ----------
class StageRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    substages: list | None = None
    position: int | None = None


@router.get("/knowledge/stages", dependencies=admin_only)
def get_stages():
    return {"stages": stages.list_stages()}


@router.post("/knowledge/stages", dependencies=admin_only)
def create_stage(req: StageRequest):
    try:
        return stages.create_stage(req.title, req.description or "", req.substages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/knowledge/stages/{stage_id}", dependencies=admin_only)
def update_stage(stage_id: str, req: StageRequest):
    if stages.get_stage(stage_id) is None:
        raise HTTPException(status_code=404, detail="Этап не найден")
    try:
        return stages.update_stage(stage_id, **req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/knowledge/stages/{stage_id}", dependencies=admin_only)
def delete_stage(stage_id: str):
    if not stages.delete_stage(stage_id):
        raise HTTPException(status_code=404, detail="Этап не найден")
    return {"deleted": True}


# ---------- Вопросы без ответа (эскалация человеку) ----------
class ResolveRequest(BaseModel):
    answer: str
    # Положить ответ в базу ответов: следующий похожий вопрос получит его мгновенно.
    # base_question — вопрос в общем виде (без имён и личных подробностей сотрудника).
    add_to_base: bool = False
    base_question: str | None = None


@router.get("/questions", dependencies=admin_only)
def get_questions(status: str | None = "open"):
    """Очередь вопросов сотрудников, на которые ассистент не ответил сам."""
    return {"questions": questions.list_all(status=status or None),
            "open_count": questions.count_open()}


@router.post("/questions/{qid}/resolve", dependencies=admin_only)
def resolve_question(qid: str, req: ResolveRequest, actor: dict = Depends(require_admin)):
    """Администратор отвечает на вопрос — ответ уходит в личный кабинет сотрудника."""
    try:
        entry = questions.resolve(qid, req.answer, actor.get("full_name") or actor.get("username"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    # Уведомляем сотрудника об ответе: кладём в его инбокс (видно в приложении) + push.
    if entry.get("user_id"):
        try:
            import messaging
            messaging.deliver_now(
                entry["user_id"], "Ответ на ваш вопрос",
                f"«{entry['question']}» — {entry['answer']}",
                {"question_id": qid},
            )
        except Exception as e:
            print(f"[questions] уведомление об ответе не отправлено: {e}")
    if req.add_to_base:
        base_q = (req.base_question or "").strip() or entry.get("resolved_question") or entry["question"]
        qacache.put(base_q, "", {"answer": entry["answer"]}, source="human")
    return entry


# ---------- База готовых ответов (qacache) ----------
class QaEditRequest(BaseModel):
    question: str
    answer: str


@router.get("/qa-base", dependencies=admin_only)
def qa_base(source: str | None = None):
    """Все готовые ответы: частые вопросы, по секциям, ответы ассистента и специалистов."""
    return {"items": qacache.list_all(source=source or ""), "stats": qacache.stats()}


@router.put("/qa-base/{qa_id}", dependencies=admin_only)
def qa_base_edit(qa_id: int, req: QaEditRequest, actor: dict = Depends(require_admin)):
    try:
        item = qacache.update(qa_id, req.question, req.answer,
                              actor.get("full_name") or actor.get("username") or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if item is None:
        raise HTTPException(status_code=404, detail="Ответ не найден")
    return item


@router.delete("/qa-base/{qa_id}", dependencies=admin_only)
def qa_base_delete(qa_id: int):
    if not qacache.delete(qa_id):
        raise HTTPException(status_code=404, detail="Ответ не найден")
    return {"deleted": True}


@router.post("/qa-base/refresh", dependencies=admin_only)
def qa_base_refresh():
    """Догенерировать частые вопросы по новым/изменённым документам (в фоне)."""
    import jobs
    return {"queued": jobs.enqueue_faq_refresh()}
