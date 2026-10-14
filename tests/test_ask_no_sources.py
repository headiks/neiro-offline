"""Ответ ассистента сотруднику: без названий документов-источников и без подробностей
устройства системы; администратору источники остаются (отладка). Без сети и БД."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_chat
import deps

RESULT = {"question": "Кто мой наставник?", "classification": {}, "route": "rag", "candidates": [{"x": 1}],
          "top_fragments": ["фрагмент регламента"], "answer": "Уточните у мастера участка.",
          "sources": [{"source": "00_Программа_адаптации_90_дней.pdf"}], "elapsed_time": 0.1}


def _client(monkeypatch, role):
    monkeypatch.setattr(api_chat, "handle_question", lambda *a, **k: dict(RESULT))
    monkeypatch.setattr(api_chat, "_route_to_human", lambda r, u: r)
    monkeypatch.setattr(api_chat, "_current_stage_ids", lambda u: [])
    monkeypatch.setattr(api_chat.security, "limit", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(api_chat.router)
    user = {"id": f"u-{role}", "role": role, "position": "Водитель"}
    for dep in {deps.require_setup_done, *[d.dependency for d in deps.logged_in]}:
        app.dependency_overrides[dep] = lambda: user
    return TestClient(app)


def test_employee_gets_no_sources(monkeypatch):
    d = _client(monkeypatch, "employee").post("/ask", json={"question": "Кто мой наставник?"}).json()
    assert d["answer"] == "Уточните у мастера участка."
    assert d["sources"] == [] and d["top_fragments"] == [] and d["candidates"] == []


def test_admin_keeps_sources(monkeypatch):
    d = _client(monkeypatch, "admin").post("/ask", json={"question": "Кто мой наставник?"}).json()
    assert d["sources"] == ["00_Программа_адаптации_90_дней.pdf"]


def test_canned_replies_do_not_mention_cabinet_or_documents():
    for text in (api_chat.ESCALATE_REPLY, api_chat.NO_ANSWER_REPLY):
        low = text.lower()
        assert "кабинет" not in low and "регламент" not in low and "документ" not in low


def test_cached_answer_without_route_passes_response_model(monkeypatch):
    """Готовый ответ faq/section/human несёт в meta только sources — /ask не должен падать."""
    import qacache
    import rag
    monkeypatch.setattr(rag.deepseek, "offline", lambda: False)
    monkeypatch.setattr(rag, "resolve_question", lambda q, h: {"standalone_question": q, "context_used": False})
    monkeypatch.setattr(qacache, "get", lambda q, p: {"answer": "Ответ специалиста.", "qa_id": 1,
                                                      "qa_source": "human"})
    r = rag.handle_question("Где получить пропуск?")
    assert r["route"] == "rag" and r["classification"]["cached"] is True
    api_chat.QuestionResponse(**{**r, "session_id": "s1"})   # не поднимает ValidationError
