"""Доска «этапы ↔ документы» по выбранному плану (свой план тоже): подэтапы плана получают
документы своих тем каталога, подэтап без документов виден как недостающий."""
import test_stubs
test_stubs.install(psycopg=True, stub_modules=())
import documents  # noqa: E402
import planner  # noqa: E402

PLAN = {"stages": [
    {"id": "p1", "title": "Первый день", "catalog_id": "day1", "substages": [
        {"id": "a", "title": "Пропуск", "catalog_id": "pass"},
        {"id": "b", "title": "Охрана труда", "catalog_id": "safety"}]},
    {"id": "p2", "title": "Свой этап", "substages": [
        {"id": "c", "title": "Своя тема", "topic_keys": ["day1.pass", "week1.it"]}]}]}
DOCS = [{"sha256": "1", "filename": "pass.pdf", "substages": [{"stage_id": "day1", "substage_id": "day1.pass", "score": 0.9}]},
        {"sha256": "2", "filename": "other.pdf", "substages": [{"stage_id": "x", "substage_id": "x.y", "score": 0.8}]}]


def test_board_by_plan():
    b = documents.build_board(*planner.plan_board(PLAN, DOCS))
    subs = {s["id"]: [d["filename"] for d in s["documents"]] for st in b["stages"] for s in st["substages"]}
    assert subs == {"a": ["pass.pdf"], "b": [], "c": ["pass.pdf"]}          # b — недостающий
    assert [d["filename"] for d in b["unassigned"]] == ["other.pdf"]        # не из этого плана
