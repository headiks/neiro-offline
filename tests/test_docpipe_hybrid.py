"""
Гибридная разметка docpipe: основа без ИИ (lexical.py — Snowball, TextTiling, C99, разметка
текста, TF-IDF) и выбор моделью только из кандидатов. Без сети и БД.
"""
import json

import test_stubs

test_stubs.install(db=True, psycopg=True)

from docpipe import lexical, pipeline  # noqa: E402

SIZ = ["Работнику выдаются средства индивидуальной защиты: каска, перчатки, спецобувь.",
       "Выдача СИЗ производится на складе по личной карточке учёта.",
       "Срок носки спецодежды определяется нормами выдачи СИЗ для профессии.",
       "При износе каски или перчаток работник получает новый комплект СИЗ на складе.",
       "Без полного комплекта средств защиты работник к работе не допускается.",
       "Спецобувь и спецодежда подбираются по размеру при первой выдаче на складе."]
PAY = ["Заработная плата состоит из оклада, премии и районного коэффициента.",
       "Премия начисляется ежемесячно по итогам работы и выполнению плана.",
       "Аванс выплачивается 25 числа, окончательный расчёт — 10 числа следующего месяца.",
       "Размер премии может быть снижен при нарушении трудовой дисциплины.",
       "Районный коэффициент и северная надбавка начисляются на оклад и премию.",
       "Расчётный листок с суммами начислений выдаётся в бухгалтерии."]
FIRE = ["При обнаружении пожара немедленно сообщите по телефону 112 и мастеру участка.",
        "Огнетушители расположены у выхода из цеха и на посту мастера.",
        "Эвакуация проводится по планам эвакуации, вывешенным на каждом этаже.",
        "Курение на территории запрещено, кроме специально отведённых мест.",
        "Пожарный инструктаж проводится при приёме на работу и раз в полгода.",
        "Запрещено загромождать проходы и эвакуационные выходы."]
TEXT = "\n".join(SIZ + PAY + FIRE)


def _catalog():
    cat = json.load(open("data/stage_catalog.json", encoding="utf-8"))
    structure = {"stages": [{"id": st["id"], "title": st["title"], "substages": [
        {"id": f"{st['id']}.{s['id']}", "title": s["title"], "description": s.get("brief", "")}
        for s in st["substage_templates"]]} for st in cat["stages"]]}
    extra = {f"{st['id']}.{s['id']}": {"tags": s.get("tags") or [], **(s.get("focus") or {})}
             for st in cat["stages"] for s in st["substage_templates"]}
    return structure, extra


def test_stemmer_conflates_word_forms():
    for forms in (("инструктаж", "инструктажа", "инструктажем"), ("работник", "работника", "работникам"),
                  ("безопасность", "безопасности"), ("охране", "охрану")):
        assert len({lexical.stem(w) for w in forms}) == 1, [lexical.stem(w) for w in forms]
    assert ("СИЗ", "СИЗ") in lexical.tokens("Выдача СИЗ")          # аббревиатура — как есть


def test_segmentation_votes_split_three_topics():
    segs = lexical.segment(TEXT, max_chars=1500, min_chars=200)
    assert [s["text"].split()[0] for s in segs] == ["Работнику", "Заработная", "При"]
    assert segs[0]["votes"] == {} and all(sum(s["votes"].values()) >= 2 for s in segs[1:])


def test_numbered_clauses_are_a_cue():
    us = lexical.units("5.1 Общие требования к одежде.\nРаботник носит каску.\nРАЗДЕЛ ВТОРОЙ ОПЛАТА")
    assert [u["cue"] for u in us] == [True, False, True]


def test_matcher_finds_topic_without_any_model():
    structure, extra = _catalog()
    segs = lexical.segment(TEXT, max_chars=1500, min_chars=200)
    m = lexical.Matcher(lexical.plan_profiles(structure, extra), [s["text"] for s in segs])
    siz, pay = (m.candidates(s["text"]) for s in segs[:2])
    assert "средств защиты" in siz[0]["title"] and siz[0]["confidence"] > 0.5, siz[:2]
    assert "доход" in pay[0]["title"].lower(), pay[:2]
    words = [w for w, _ in lexical.keywords(segs[1]["text"], m.idf, 5)]
    assert "премии" in words or "премия" in words, words


def test_hybrid_label_without_model_keeps_full_output(monkeypatch):
    monkeypatch.setattr(pipeline, "REFINE", "off")
    structure, extra = _catalog()
    m = lexical.Matcher(lexical.plan_profiles(structure, extra), [TEXT])
    out = pipeline._hybrid_label({"text": TEXT}, set(), structure, ["Оператор станков с ЧПУ"], m)
    assert out["section"]["substages"] and out["section"]["why"].startswith("Ключевые слова:")
    assert len(out["chunks"]) == 3
    meta = out["chunks"][0]["meta"]
    assert meta["candidates"] and meta["keywords"] and meta["picked_by"] == "lexical"


def test_model_only_chooses_among_candidates(monkeypatch):
    cands = [{"id": "a", "title": "A", "score": 0.12, "confidence": 0.48},
             {"id": "b", "title": "B", "score": 0.11, "confidence": 0.44}]
    monkeypatch.setattr(pipeline, "_refine", lambda text, c, m: ["b"])
    subs, by = pipeline._choose("текст", cands, None)
    assert subs == [{"id": "b", "confidence": 0.6}] and by == pipeline.REFINE
    monkeypatch.setattr(pipeline, "_refine", lambda text, c, m: None)       # модель недоступна
    assert pipeline._choose("текст", cands, None) == ([{"id": "a", "confidence": 0.48}], "lexical")


def test_refine_ignores_ids_outside_candidates(monkeypatch):
    import deepseek
    monkeypatch.setattr(pipeline, "REFINE", "local")
    monkeypatch.setattr(pipeline, "_refine_off_until", 0.0)
    monkeypatch.setattr(deepseek, "chat", lambda *a, **k: '{"pick": ["b", "выдуманный"]}')
    m = type("M", (), {"profiles": [{"id": "a"}, {"id": "b"}]})()
    assert pipeline._refine("текст", [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}], m) == ["b"]


def test_boilerplate_and_professions():
    assert lexical.is_boilerplate("Настоящее положение разработано в соответствии с ТК РФ.")
    assert not lexical.is_boilerplate("Каска выдаётся на складе.")
    assert lexical.professions_in("Операторам станков с ЧПУ выдаются очки.", ["Оператор станков с ЧПУ", "Водитель"]) \
        == ["Оператор станков с ЧПУ"]
