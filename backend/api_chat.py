"""Чат с ассистентом: вопрос -> RAG -> ответ, память диалога, эскалация человеку."""

from fastapi import APIRouter, Depends, HTTPException, Request
import json
import os
import time
import uuid
import threading
from collections import OrderedDict, deque

from redis_conn import get_redis

from pydantic import BaseModel, Field

import users
import security
import questions
from rag import handle_question, HISTORY_WINDOW
from deps import require_setup_done, logged_in

router = APIRouter()

# Длинный «вопрос» — это не вопрос, а способ сжечь токены DeepSeek.
QUESTION_MAX_CHARS = 2000
# Вопросов к ИИ от одного пользователя в минуту (каждый — 2–3 запроса к модели).
ASK_RATE_PER_MIN = int(os.environ.get("NEIROMASTER_ASK_RATE", "20"))


class QuestionRequest(BaseModel):
    question: str = Field(max_length=QUESTION_MAX_CHARS)
    session_id: str | None = Field(default=None, max_length=64)   # нет — сервер создаст новый


class QuestionResponse(BaseModel):
    question: str
    resolved_question: str | None = None   # вопрос, переформулированный с учётом истории (если применимо)
    context_used: bool = False             # был ли использован контекст предыдущих вопросов
    session_id: str
    classification: dict
    route: str
    candidates: list
    top_fragments: list
    answer: str | None = None
    escalated: bool = False                 # вопрос без ответа передан администратору
    sources: list[str] = []                 # названия документов, по которым дан ответ
    elapsed_time: float
    error: str | None = None


# ---------- Память диалога по сессиям (in-memory) ----------
# Хранит последние вопросы/ответы для каждого session_id — используется, чтобы
# handle_question мог разрешать контекстные вопросы вроде "Где взять".
# Живёт только в памяти текущего процесса: подходит для одного uvicorn-воркера;
# для многопроцессного/многосерверного деплоя нужно вынести в Redis или аналог.
HISTORY_MAX_STORE = 20  # сколько реплик хранить на сессию (окно анализа контекста меньше — HISTORY_WINDOW)
# Верхняя граница числа сессий в памяти. Без неё словарь рос бы бесконечно (каждая
# новая вкладка = новый session_id), медленно утекая по памяти. При переполнении
# вытесняется самая давно не активная сессия (LRU) — её история просто пересоздастся.
MAX_SESSIONS = 5000

_history_lock = threading.Lock()
_conversation_history: "OrderedDict[str, deque]" = OrderedDict()
_session_owner: dict[str, str] = {}   # session_id -> user_id первого владельца сессии


def _evict_sessions_locked():
    """Держим не больше MAX_SESSIONS сессий. Вызывать под _history_lock."""
    while len(_conversation_history) > MAX_SESSIONS:
        old_sid, _ = _conversation_history.popitem(last=False)
        _session_owner.pop(old_sid, None)


# При нескольких web-воркерах история должна быть общей, иначе контекстные вопросы
# («Где взять?») ломаются, когда следующий запрос попал в другой воркер. Есть Redis —
# храним историю и владельца там (список + ключ, TTL сутки); нет — in-memory (как раньше).
_HIST_TTL = 24 * 3600


def _hist_key(sid: str) -> str:
    return f"nmhist:{sid}"


def _owner_key(sid: str) -> str:
    return f"nmowner:{sid}"


def get_session_owner(session_id: str) -> str | None:
    r = get_redis()
    if r is not None:
        try:
            return r.get(_owner_key(session_id))
        except Exception:
            pass
    with _history_lock:
        return _session_owner.get(session_id)


def get_recent_history(session_id: str, n: int = HISTORY_WINDOW) -> list:
    r = get_redis()
    if r is not None:
        try:
            raw = r.lrange(_hist_key(session_id), -n, -1)
            return [json.loads(x) for x in raw]
        except Exception:
            pass
    with _history_lock:
        hist = list(_conversation_history.get(session_id, []))
    return hist[-n:]


def append_history(session_id: str, question: str, answer: str | None, owner_id: str | None = None):
    r = get_redis()
    if r is not None:
        try:
            k = _hist_key(session_id)
            r.rpush(k, json.dumps({"question": question, "answer": answer}, ensure_ascii=False))
            r.ltrim(k, -HISTORY_MAX_STORE, -1)
            r.expire(k, _HIST_TTL)
            if owner_id:
                r.set(_owner_key(session_id), owner_id, nx=True, ex=_HIST_TTL)
            return
        except Exception:
            pass
    with _history_lock:
        dq = _conversation_history.get(session_id)
        if dq is None:
            dq = deque(maxlen=HISTORY_MAX_STORE)
            _conversation_history[session_id] = dq
        else:
            _conversation_history.move_to_end(session_id)   # активная сессия — в конец очереди LRU
        dq.append({"question": question, "answer": answer})
        if owner_id and session_id not in _session_owner:
            _session_owner[session_id] = owner_id
        _evict_sessions_locked()


def _current_stage_ids(user: dict) -> list:
    """Текущий этап обучения пользователя (ТЗ §6) — приоритет поиска, не фильтр.
    ponytail: пока прогресс обучения по этапам-блокам отдельно не трекается, отдаём
    пусто (поиск работает без буста). Точка интеграции, когда появится прогресс:
    вернуть id этапов из stages, на которых сейчас пользователь."""
    return []


# ---------- RAG-вопросы ----------
# Без подробностей устройства системы (кабинет, документы) — только что происходит дальше.
ESCALATE_REPLY = "⚠️ Вопрос требует внимания специалиста — передал его ответственному, он скоро ответит."
NO_ANSWER_REPLY = "Точного ответа у меня пока нет — передал вопрос специалисту, он скоро ответит."


def _route_to_human(result: dict, user: dict) -> dict:
    """
    Вопрос без ответа не теряем: ставим в очередь администратору и показываем
    сотруднику понятное сообщение вместо пустого ответа/технической ошибки.
    Срабатывает для escalate (ЧС) и для rag без найденного ответа. ЧС по регэкспу
    (result["emergency"]) уже несёт инструкцию — её сохраняем, но вопрос всё равно
    ставим в очередь человеку.
    """
    emergency = result.get("emergency")
    if not emergency and (result.get("answer") or result.get("route") not in ("rag", "escalate")):
        return result
    cls = result.get("classification") or {}
    reason = questions.REASON_ESCALATE if (result["route"] == "escalate" or cls.get("risk_flag")) \
        else questions.REASON_NO_ANSWER
    questions.record(user, result["question"], result.get("resolved_question"),
                     reason, cls.get("risk_type"))
    result["escalated"] = True
    result["error"] = None  # «нет кандидатов» — не ошибка для пользователя, это эскалация
    if not result.get("answer"):
        result["answer"] = ESCALATE_REPLY if reason == questions.REASON_ESCALATE else NO_ANSWER_REPLY
    return result


def _source_names(sources) -> list:
    names = []
    for s in sources or []:
        name = s.get("source") if isinstance(s, dict) else s
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names[:3]


# Синхронный def (не async): handle_question ходит в DeepSeek синхронными
# requests на секунды-минуты. В async-обработчике это заблокировало бы весь event loop
# uvicorn-воркера — «зависли» бы все параллельные запросы. Обычный def FastAPI выполняет
# в threadpool, поэтому воркер продолжает обслуживать других пользователей.
@router.post("/ask", response_model=QuestionResponse, dependencies=logged_in)
def ask(req: QuestionRequest, request: Request, user: dict = Depends(require_setup_done)):
    start = time.time()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Вопрос не может быть пустым")
    security.limit(request, "ask", ASK_RATE_PER_MIN, 60, key=user["id"])

    # session_id связывает подряд идущие вопросы в один диалог. Фронтенд генерирует
    # его один раз на вкладку и присылает с каждым запросом; если его нет — заводим новый.
    # Чужой session_id не принимаем: иначе в контекст попала бы переписка другого человека.
    session_id = req.session_id or str(uuid.uuid4())
    owner = get_session_owner(session_id)
    if owner and owner != user["id"]:
        session_id = str(uuid.uuid4())
    history = get_recent_history(session_id)

    try:
        result = handle_question(question, history=history, current_stage_ids=_current_stage_ids(user),
                                 position=user.get("position"))
        result = _route_to_human(result, user)
        result["elapsed_time"] = time.time() - start
        result["session_id"] = session_id
        append_history(session_id, question, result.get("answer"), owner_id=user["id"])
        # Документы, по которым дан ответ, — только администратору (отладка). Сотруднику
        # не показываем ни названий, ни фрагментов: ему нужен ответ, а не устройство базы.
        result["sources"] = [] if result.get("escalated") else _source_names(result.get("sources"))
        if not users.is_admin(user):
            result["sources"] = []
            result["top_fragments"] = []
            result["candidates"] = []
        return QuestionResponse(**result)
    except Exception as e:
        # Внутреннюю причину — только в лог сервера, наружу общее сообщение:
        # str(e) может раскрывать детали инфраструктуры (адреса, схемы, стек).
        print(f"[ASK] ошибка обработки вопроса (session={session_id}): {e!r}")
        return QuestionResponse(
            question=question,
            session_id=session_id,
            classification={},
            route="error",
            candidates=[],
            top_fragments=[],
            answer=None,
            elapsed_time=time.time() - start,
            error="Не удалось обработать вопрос. Попробуйте ещё раз позже."
        )


@router.delete("/session/{session_id}")
def reset_session(session_id: str, user: dict = Depends(require_setup_done)):
    """Очищает историю диалога для сессии (например, при нажатии «Новый диалог» на сайте).
    Чужую сессию чистить нельзя — иначе любой вошедший стирал бы историю по чужому id."""
    owner = get_session_owner(session_id)
    if owner and owner != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваша сессия")
    r = get_redis()
    if r is not None:
        try:
            existed = bool(r.exists(_hist_key(session_id)))
            r.delete(_hist_key(session_id), _owner_key(session_id))
            return {"session_id": session_id, "cleared": existed}
        except Exception:
            pass
    with _history_lock:
        existed = session_id in _conversation_history
        _conversation_history.pop(session_id, None)
        _session_owner.pop(session_id, None)
    return {"session_id": session_id, "cleared": existed}
