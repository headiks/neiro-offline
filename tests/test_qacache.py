"""
База готовых ответов (qacache) и генерация частых вопросов (faq):
  - перефраз порядком слов и пунктуацией — точное совпадение; по смыслу — через векторы;
  - ответ своей должности выигрывает у общего, чужой должности не виден;
  - FAQ генерируется один раз; изменился текст подэтапа — FAQ и ответы модели по нему
    пересоздаются, ответы специалиста остаются.

Векторы — детерминированная заглушка (смысл = ключевое слово), модель — заглушка.
Логика ранжирования проверяется без БД; остальное — на тестовой PostgreSQL
(NEIROMASTER_TEST_DSN), нет БД — пропускается.
"""
import os
import tempfile

import numpy as np
import pytest

TEST_DSN = (os.environ.get("NEIROMASTER_TEST_DSN")
            or "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_test")
os.environ.update({
    "NEIROMASTER_DB_DSN": TEST_DSN,
    "REDIS_URL": "",
    "NEIROMASTER_EMBED_URL": "http://embed.test",
    "NEIROMASTER_PII_KEY": "",
    "NEIROMASTER_PII_KEY_FILE": os.path.join(tempfile.mkdtemp(), "secrets", "pii.key"),
})

import embed  # noqa: E402
import qacache  # noqa: E402

MEANINGS = ["пропуск", "спецодежд", "зарплат", "аванс"]


def fake_vectors(texts):
    out = []
    for t in texts:
        v = np.zeros(len(MEANINGS) + 1, dtype=np.float32)
        hits = [i for i, m in enumerate(MEANINGS) if m in t.lower()]
        for i in hits:
            v[i] = 1.0
        if not hits:
            v[-1] = 1.0
        out.append(v / np.linalg.norm(v))
    return np.asarray(out)


embed.vectors = fake_vectors


def test_best_match_prefers_own_position_and_hides_others():
    mat = fake_vectors(["пропуск", "пропуск", "пропуск", "спецодежда"])
    ids, pos = np.array([1, 2, 3, 4]), ["", "водитель", "сварщик", ""]
    q = fake_vectors(["где пропуск"])[0]
    assert qacache.best_match(mat, ids, pos, q, "Водитель")[0] == 2
    assert qacache.best_match(mat, ids, pos, q, "")[0] == 1          # чужие должности не видны
    assert qacache.best_match(mat, ids, ["сварщик"] * 4, q, "") is None


def test_normalize_ignores_order_and_punctuation():
    assert qacache.normalize("Где получить пропуск?") == qacache.normalize("пропуск: где ПОЛУЧИТЬ")
    assert qacache.normalize("Мне не выдали пропуск") != qacache.normalize("Мне выдали пропуск")


try:
    import psycopg
    with psycopg.connect(TEST_DSN, connect_timeout=3) as _c:
        _c.execute("DROP TABLE IF EXISTS qa_answers, qa_state, section_labels, chunks, sections, "
                   "label_jobs, documents, plan_versions CASCADE")
    DB_OK = True
except Exception:                    # noqa: BLE001 — любая причина = нет БД
    DB_OK = False

db_only = pytest.mark.skipif(not DB_OK, reason="тестовая PostgreSQL недоступна")


@pytest.fixture(scope="module")
def base():
    import db
    import docpipe
    import pii_key
    db.configure(TEST_DSN)
    pii_key.ensure()
    docpipe.init_schema()
    qacache.init()
    import docindex
    docindex.init()
    yield


@db_only
def test_exact_semantic_and_position(base):
    qacache.put("Где получить пропуск?", "", {"answer": "У охраны, корпус 1"}, source="faq")
    qacache.put("Когда выдают зарплату?", "Водитель", {"answer": "10 и 25 числа"}, source="model")

    hit = qacache.get("пропуск: где ПОЛУЧИТЬ", "Сварщик")               # точное, общий ответ
    assert hit["answer"] == "У охраны, корпус 1" and hit["qa_score"] == 1.0
    assert qacache.get("Куда идти за пропуском?", "")["qa_source"] == "faq"   # по смыслу
    assert qacache.get("Где получить спецодежду?", "") is None
    assert qacache.get("Когда зарплата", "водитель")["answer"] == "10 и 25 числа"
    assert qacache.get("Когда зарплата", "Сварщик") is None             # чужая должность

    row = qacache.list_all(source="faq")[0]
    assert row["hits"] == 2
    # Вопрос и ответ в БД зашифрованы, наружу — открытым текстом.
    import db
    raw = db.query("SELECT question FROM qa_answers WHERE id = %s", (row["id"],), "one")
    assert raw["question"] != "Где получить пропуск?" and row["question"] == "Где получить пропуск?"


@db_only
def test_faq_generated_once_and_refreshed_on_change(base, monkeypatch):
    import db
    import faq
    import planner
    import rag

    monkeypatch.setattr(planner, "load_catalog", lambda: {"stages": [
        {"id": "s1", "title": "Первый день", "substage_templates": [{"id": "pass", "title": "Пропуск"}]}]})
    calls = {"q": 0, "a": 0}

    def small_llm(system, user, step_name=""):
        calls["q"] += 1
        return '{"questions": ["Где получить пропуск на завод?", "Что взять для аванса?"]}'

    def generate_answer(question, ctx, position=""):
        calls["a"] += 1
        if "аванс" in question:
            return "Точных сведений об этом у меня нет — уточните у HR."   # не попадёт в базу
        return "Ответ: " + ("на проходной" if any("проходной" in c for c in ctx) else "в бюро")

    monkeypatch.setattr(rag, "small_llm", small_llm)
    monkeypatch.setattr(rag, "generate_answer", generate_answer)
    db.execute("DELETE FROM qa_answers")

    db.execute("INSERT INTO documents (id, filename, content_hash) VALUES ('d1', 'pass.pdf', 'h1')")
    db.execute("INSERT INTO sections (id, doc_id, seq, text) VALUES ('sec1', 'd1', 0, %s)",
               ("Пропуск выдаёт бюро пропусков в корпусе 1. " * 10,))
    db.execute("INSERT INTO section_labels (section_id, substages) VALUES ('sec1', %s::jsonb)",
               ('[{"id": "s1.pass"}]',))
    qacache.put("Как продлить пропуск?", "", {"answer": "старый ответ модели"},
                source="model", substages=["s1.pass"])
    qacache.put("Где бюро пропусков?", "", {"answer": "ответ специалиста"}, source="human")

    faq.refresh()
    assert qacache.stats() == {"faq": 1, "section": 1, "human": 1}   # ответ модели удалён
    assert calls == {"q": 2, "a": 4}

    faq.refresh()                                        # ничего не изменилось — модель не зовём
    assert calls == {"q": 2, "a": 4}

    db.execute("UPDATE sections SET text = %s WHERE id = 'sec1'", ("Пропуск — на проходной. " * 20,))
    faq.refresh()                                        # подэтап изменился — FAQ заново
    assert calls["q"] == 3
    stats = qacache.stats()
    assert stats["faq"] == 1 and stats["human"] == 1
    assert qacache.list_all(source="faq")[0]["answer"] == "Ответ: на проходной"

    db.execute("DELETE FROM sections WHERE id = 'sec1'")  # документ пересобран — старых секций нет
    faq.refresh_sections()
    assert "section" not in qacache.stats()


@db_only
def test_docindex_build_and_hybrid_search(base):
    import db
    import docindex
    docindex.init()
    db.execute("INSERT INTO documents (id, filename, content_hash) VALUES ('d2', 'wear.pdf', 'h2') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO sections (id, doc_id, seq, text) VALUES ('sec2', 'd2', 0, %s)",
               ("Спецодежду выдают на складе в первый рабочий день.\n\nЗарплату перечисляют на карту.",))
    db.execute("INSERT INTO section_labels (section_id, substages) VALUES ('sec2', %s::jsonb)",
               ('[{"id": "s1.wear"}]',))
    assert docindex.build() >= 1
    assert docindex.build() == 0                                     # повторно — ничего нового
    hits = docindex.search("Где выдают спецодежду?", k=1)
    assert hits and "Спецодежду" in hits[0]["text"] and hits[0]["substages"] == ["s1.wear"]
    assert docindex.stats()["sections_without_chunks"] == 0
