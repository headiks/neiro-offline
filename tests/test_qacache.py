"""Кэш частых вопросов: перефраз порядком слов/пунктуацией попадает в один ответ,
смена документов (версия базы) сбрасывает кэш. Без Redis — память процесса."""
import os
os.environ["REDIS_URL"] = ""
import qacache  # noqa: E402


def test_same_meaning_hits_and_invalidation():
    qacache.put("Где получить пропуск?", "", {"answer": "У охраны"})
    assert qacache.get("пропуск: где ПОЛУЧИТЬ", "")["answer"] == "У охраны"
    assert qacache.get("Где получить пропуск?", "Водитель") is None     # другая должность
    assert qacache.get("Где получить спецодежду?", "") is None
    qacache.bump_version()                                              # документы изменились
    assert qacache.get("Где получить пропуск?", "") is None
