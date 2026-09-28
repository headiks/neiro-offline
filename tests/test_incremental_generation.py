"""Инкрементальная генерация: модель зовётся только при изменении входов подэтапа
(документы, бриф, должность). Без сети/БД: docpipe и rag подменены стабами."""
import sys
import types

BLOCKS = {"st.sub": [{"text": "Пропуск выдаёт охрана.", "source": "a.pdf"}]}
CALLS = []
sys.modules["docpipe"] = types.SimpleNamespace(blocks_for_substage=lambda k: BLOCKS.get(k, []))
sys.modules.setdefault("indexing", types.SimpleNamespace())
sys.modules["rag"] = types.SimpleNamespace(
    big_llm=lambda s, u: CALLS.append(u) or '{"body": "текст"}',
    parse_json_response=lambda s: {"body": "текст"},
    route_substages=lambda text, top=3: ["st.sub"])

import planner  # noqa: E402

STAGE = {"catalog_id": "st", "title": "Первый день"}
SUB = {"catalog_id": "sub", "title": "Пропуск", "brief": "Где получить пропуск", "kind": "message"}
PRESENT = {"st.sub"}


def gen(sub=SUB, prev=None, position=""):
    return planner.generate_substage_message(STAGE, sub, [], position=position,
                                             docs_present=PRESENT, prev=prev)


def test_unchanged_inputs_reuse_without_llm():
    CALLS.clear()
    first = gen()
    assert first["status"] == "generated" and len(CALLS) == 1
    second = gen(prev={**first, "message_id": "m"})
    assert second.get("reused") and len(CALLS) == 1          # повтор — без запроса


def test_changed_brief_or_document_regenerates():
    CALLS.clear()
    first = gen()
    gen(sub={**SUB, "brief": "Где и когда получить пропуск"}, prev=first)
    assert len(CALLS) == 2                                     # бриф поменялся
    BLOCKS["st.sub"] = BLOCKS["st.sub"] + [{"text": "Новый порядок.", "source": "b.pdf"}]
    gen(prev=first)
    assert len(CALLS) == 3                                     # документ поменялся
    BLOCKS["st.sub"] = BLOCKS["st.sub"][:1]


def test_legacy_and_edited_texts_are_kept():
    CALLS.clear()
    assert gen(prev={"status": "generated", "content": {"text": "старое"}})["reused"]
    assert gen(sub={**SUB, "brief": "другое"},
               prev={"status": "edited", "fingerprint": "x", "content": {"text": "руками"}})["reused"]
    assert CALLS == []


def test_no_document_skips_without_llm():
    CALLS.clear()
    r = planner.generate_substage_message(STAGE, SUB, [], docs_present=set())
    assert r["status"] == "skipped" and CALLS == []


def test_custom_substage_gets_topics_once():
    plan = {"stages": [{"title": "Свой этап", "substages": [{"id": "s1", "title": "Кофе", "brief": "где кухня"}]}]}
    routed = []
    router = lambda text, top=2: routed.append(text) or ["st.sub"]
    planner.assign_topics(plan, router=router)
    sub = plan["stages"][0]["substages"][0]
    assert sub["topic_keys"] == ["st.sub"] and len(routed) == 1
    again = {"stages": [{"title": "Свой этап", "substages": [{"id": "s1", "title": "Кофе", "brief": "где кухня"}]}]}
    planner.assign_topics(again, prev=plan, router=router)
    assert again["stages"][0]["substages"][0]["topic_keys"] == ["st.sub"] and len(routed) == 1
    # свой подэтап теперь генерируется по документам темы, а не уходит в «пропущено»
    CALLS.clear()
    r = planner.generate_substage_message({"title": "Свой этап"}, sub, [], docs_present=PRESENT)
    assert r["status"] == "generated" and len(CALLS) == 1
