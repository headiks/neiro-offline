"""Конвертер унифицированного сообщения -> опрос/тест/чек-лист/сообщение.
Чистая логика, без сети/БД. Запуск: python test_msgconvert.py"""
import msgconvert


def test_convert():
    msgconvert._demo()


if __name__ == "__main__":
    test_convert()
    print("test_msgconvert: OK")


def test_every_catalog_kind_converts():
    """Каждый тип подэтапа из каталога (мини-тест, опрос, чек-лист, напоминание…) даёт
    непустой результат своего формата — «тестовый план со всеми типами» без сети."""
    import json
    cat = json.load(open("data/stage_catalog.json", encoding="utf-8"))
    content = {"title": "Т", "intro": "Вступление", "body": "Текст", "key_points": ["п1"],
               "checklist": ["сделать 1", "сделать 2"],
               "questions": [{"text": "Сколько?", "options": ["1", {"text": "2", "correct": True}]}]}
    for kind in (k["id"] for k in cat["substage_kinds"]):
        assert kind in msgconvert.KIND_TO_FORMAT, kind
        out = msgconvert.convert(content, kind)
        fmt = msgconvert.KIND_TO_FORMAT[kind]
        assert out.get("type") == fmt, (kind, out)
        body = {"quiz": "questions", "survey": "questions", "checklist": "items", "message": "text"}[fmt]
        assert out[body], (kind, out)


def test_tidy_table_enums_paragraphs_and_terms():
    """Тест-драйв: таблица из палочек, перечень в строку, «простыня» и жаргон — чиним без модели."""
    t = msgconvert.tidy("Действуйте так:\n| Ситуация | К кому |\n|--|--|\n| Травма | Медпункт |\n| ЧП | 112 |")
    assert "|" not in t and "• Травма — Медпункт" in t and "Ситуация" not in t
    t = msgconvert.tidy("Порядок: 1) остановите станок; 2) позовите мастера; 3) сообщите руководителю.")
    assert t.split("\n") == ["Порядок:", "1) остановите станок;", "2) позовите мастера;", "3) сообщите руководителю."]
    long = " ".join(f"Предложение номер {i} про охрану труда на участке и порядок работы." for i in range(12))
    assert "\n\n" in msgconvert.tidy(long)
    t = msgconvert.tidy("Сверьтесь со штаткой и с должностной. Изучите должностную, уточните у HR. "
                        "Три недели практики под наставником.")
    assert t == ("Сверьтесь со штатным расписанием и с должностной инструкцией. Изучите должностную "
                 "инструкцию, уточните в отделе кадров. Три недели практики под руководством наставника.")
    assert msgconvert.tidy("Должностная инструкция и должностной оклад") == "Должностная инструкция и должностной оклад"
    assert msgconvert.tidy(t) == t                                     # идемпотентно


def test_contacts_only_when_asked_to_reach_out():
    roles = lambda s: [c["role"] for c in msgconvert.contacts(s)]
    assert roles("Если что-то неясно — напишите наставнику.") == ["mentor"]
    assert roles("Три недели практики под руководством наставника.") == []
    assert roles("Вопросы по комплекту — к руководителю. Уточните в отделе кадров.") == ["manager", "hr"]
    assert roles("Сообщите руководителю и обратитесь в отдел кадров.") == ["manager", "hr"]
    out = msgconvert.convert({"body": "Сомневаетесь — обратитесь к наставнику."}, "message")
    assert out["contacts"] == [{"role": "mentor", "label": "Написать наставнику"}]


def test_survey_options_buttons():
    out = msgconvert.convert({"questions": [
        {"text": "Хотите задать вопрос?", "type": "single",
         "options": ["Нет, всё понятно", "Написать наставнику", "Написать в отдел кадров"]},
        {"text": "Пропуск получен?", "type": "bool"}]}, "survey")
    q1, q2 = out["questions"]
    assert [o.get("contact") for o in q1["options"]] == [None, "mentor", "hr"]
    assert [o["text"] for o in q2["options"]] == ["Да", "Нет"]
