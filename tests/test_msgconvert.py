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
