"""
Самопроверка чистой логики реестра документов: сборка данных экрана «этапы ↔ документы»
(build_board) из LLM-разметки docpipe. Без Postgres. Косинус/векторы/эмбеддинги удалены.
Запуск: python3 test_documents.py
"""
import documents as d


def test_build_board():
    stages = [
        {"id": "s1", "title": "Первая неделя", "description": "погружение",
         "substages": [{"id": "tb", "title": "Охрана труда"}, {"id": "work", "title": "Рабочее место"}]},
        {"id": "s2", "title": "Первый месяц", "description": "самостоятельно",
         "substages": [{"id": "att", "title": "Аттестация"}]},
    ]
    docs = [
        # привязан к подэтапу tb этапа s1
        {"sha256": "h1", "filename": "ТБ.pdf", "mime": "pdf", "stage_ids": ["s1"],
         "substages": [{"stage_id": "s1", "substage_id": "tb", "score": 0.9}]},
        # отнесён к этапу s2, но без подэтапа
        {"sha256": "h2", "filename": "Приказ.pdf", "mime": "pdf", "stage_ids": ["s2"], "substages": []},
        # ни к чему -> unassigned
        {"sha256": "h3", "filename": "Этика.pdf", "mime": "pdf", "stage_ids": [], "substages": []},
    ]
    board = d.build_board(stages, docs)

    s1 = next(s for s in board["stages"] if s["id"] == "s1")
    tb = next(x for x in s1["substages"] if x["id"] == "tb")
    assert [doc["sha256"] for doc in tb["documents"]] == ["h1"]

    s2 = next(s for s in board["stages"] if s["id"] == "s2")
    assert [doc["sha256"] for doc in s2["documents"]] == ["h2"]   # в этапе, без подэтапа

    assert [doc["sha256"] for doc in board["unassigned"]] == ["h3"]
    assert board["stats"] == {"stages": 2, "substages": 3, "documents": 3, "unassigned": 1}


if __name__ == "__main__":
    test_build_board()
    print("OK: реестр документов — build_board работает")
