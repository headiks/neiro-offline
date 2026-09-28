"""
Офлайн-режим без БД и без моделей (заглушки):
  - тяжёлые операции (загрузка документов, генерация) в офлайне отказывают понятно;
  - вызовы модели идут в локальный сервер, а не в DeepSeek;
  - короткое продолжение диалога дописывается к прошлому вопросу без модели;
  - нарезка документов, слияние рангов, сжатие справки до ближайших предложений;
  - «НЕТ_ОТВЕТА» локальной модели -> вопрос уйдёт специалисту, ответ -> в базу (local).
"""
import os

import numpy as np
import pytest

os.environ.setdefault("NEIROMASTER_PII_KEY", "off")
import deepseek  # noqa: E402
import docindex  # noqa: E402
import rag  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setenv("NEIROMASTER_LLM_MODE", "offline")


def test_require_online(offline):
    with pytest.raises(deepseek.OfflineError, match="только в онлайн-режиме"):
        deepseek.require_online("Загрузка документов")


def test_require_online_passes_online(monkeypatch):
    monkeypatch.setenv("NEIROMASTER_LLM_MODE", "online")
    deepseek.require_online("Загрузка документов")


def test_chat_goes_local_offline(offline, monkeypatch):
    sent = {}

    class R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "<think>x</think> Ответ"}}], "usage": {}}

    def post(url, json=None, timeout=None, **kw):
        sent.update(url=url, body=json)
        return R()

    monkeypatch.setenv("NEIROMASTER_LOCAL_LLM_URL", "http://llm:8080")
    monkeypatch.setattr(deepseek.requests, "post", post)
    assert deepseek.chat("sys", "user", max_tokens=5000) == "Ответ"
    assert sent["url"] == "http://llm:8080/v1/chat/completions"
    assert sent["body"]["max_tokens"] <= 400                      # потолок для слабого CPU


def test_resolve_offline():
    hist = [{"question": "Где получить спецодежду?", "answer": "На складе"}]
    assert rag.resolve_offline("а когда?", hist)["standalone_question"] == "Где получить спецодежду? а когда?"
    long_q = "Сколько дней ежегодного отпуска положено работнику цеха?"
    assert rag.resolve_offline(long_q, hist) == {"standalone_question": long_q, "context_used": False}
    assert rag.resolve_offline("спасибо", hist)["context_used"] is False
    assert rag.resolve_offline("а когда?", [])["context_used"] is False


def test_split_keeps_limit_and_text():
    text = "Первый абзац. " * 40 + "\n\nВторой абзац короткий.\n" + "x" * 2000
    pieces = docindex.split(text, limit=300)
    assert all(len(p) <= 300 for p in pieces)
    assert "Второй абзац короткий." in "".join(pieces)


def test_rrf_prefers_items_in_both_rankings():
    assert docindex.rrf([[1, 2, 3], [3, 4, 1]])[:2] == [1, 3]


def test_compress_keeps_closest_sentences(monkeypatch):
    def vectors(texts):
        return np.asarray([[1.0, 0.0] if "пропуск" in t.lower() else [0.0, 1.0] for t in texts],
                          dtype=np.float32)
    monkeypatch.setattr(docindex.embed, "vectors", vectors)
    text = ("Столовая работает с восьми до пяти вечера. Пропуск выдают в бюро пропусков корпуса 1. "
            "Парковка для сотрудников бесплатная всегда. Для пропуска нужен паспорт и приказ о приёме.")
    out = docindex.compress("Где получить пропуск?", [text], budget=120)
    assert "бюро пропусков" in out and "паспорт" in out and "Столовая" not in out


def _stub_offline(monkeypatch, reply):
    import qacache
    saved = []
    monkeypatch.setattr(qacache, "nearest", lambda q, pos="", k=3: [
        {"question": "Где пропуск?", "answer": "В бюро пропусков.", "score": 0.8}])
    monkeypatch.setattr(docindex, "search", lambda q, k=3: [
        {"id": 1, "text": "Пропуск выдают в корпусе 1.", "filename": "a.pdf", "substages": ["s1.p"]}])
    monkeypatch.setattr(docindex, "compress", lambda q, texts, budget: "\n".join(texts)[:budget])
    monkeypatch.setattr(deepseek, "chat", lambda *a, **k: reply)
    monkeypatch.setattr(qacache, "put", lambda *a, **k: saved.append((a, k)))
    return saved


def test_answer_offline_no_answer_goes_to_specialist(monkeypatch):
    saved = _stub_offline(monkeypatch, "НЕТ_ОТВЕТА")
    res = rag.answer_offline("Где взять пропуск на завод?")
    assert res["answer"] is None and res["similar"] == ["Где пропуск?"] and not saved


def test_answer_offline_answer_is_saved_as_local(monkeypatch):
    saved = _stub_offline(monkeypatch, "Пропуск выдают в корпусе 1.")
    res = rag.answer_offline("Где взять пропуск на завод?", "Водитель")
    assert res["answer"] == "Пропуск выдают в корпусе 1."
    (args, kwargs), = saved
    assert kwargs == {"source": "local", "substages": ["s1.p"]} and args[1] == "Водитель"


def test_answer_with_trailing_marker_is_kept(monkeypatch):
    _stub_offline(monkeypatch, "Увольнять на больничном нельзя. НЕТ_ОТВЕТА")
    assert rag.answer_offline("Можно ли уволить на больничном?")["answer"] == "Увольнять на больничном нельзя."
