"""Доставка сообщений плана: тестовые уведомления больше не блокируют рассылку плана,
несгенерированные сообщения не уходят пустыми, тип и структура сообщения сохраняются.
Без БД: db подменён записывающей заглушкой."""
import test_stubs
test_stubs.install(psycopg=True, stub_modules=())
import sys  # noqa: E402

import messaging  # noqa: E402

MSG = {"message_id": "st1.s1", "schedule": {"send_at": "2026-09-10T09:00"},
       "stage": {"id": "st1", "title": "Первый день"},
       "substage": {"id": "s1", "title": "Чек-лист", "kind": "checklist"},
       "content": {"text": "Сделайте:", "converted": {"type": "checklist", "items": [{"id": "i1", "text": "Пропуск"}]}}}


def test_row_keeps_kind_and_structure():
    row = messaging._message_row("e1", "p1", MSG, "Europe/Moscow")
    assert row["kind"] == "checklist" and row["payload"]["items"][0]["text"] == "Пропуск"


def test_not_generated_message_is_not_sent_empty():
    empty = {**MSG, "content": {"text": ""}}
    assert messaging._message_row("e1", "p1", empty, "Europe/Moscow") is None


def test_test_notification_does_not_block_plan(monkeypatch):
    # Отбор — в SQL (см. test_api_integration.test_ensure_all_ignores_test_notifications на
    # реальной БД): строки без plan_id (тесты, ответы на вопросы) не считаются строками плана.
    seen = {}
    monkeypatch.setattr(messaging.db, "query",
                        lambda sql, *a, **k: seen.setdefault("sql", sql) and [{"id": "e1"}], raising=False)
    made = []
    monkeypatch.setattr(messaging.users, "from_row", lambda r: r, raising=False)
    monkeypatch.setattr(messaging, "materialize_employee", lambda u, force=False: made.append(u["id"]) or 1)
    assert messaging.ensure_all() == 1 and made == ["e1"]
    assert "s.plan_id = u.plan_id" in seen["sql"]

def test_push_body_hints_interactive_kinds():
    assert messaging.push_body("quiz", "Вопросы").startswith("Мини-тест")
    assert messaging.push_body("message", "Привет") == "Привет"


def test_every_kind_has_sample():
    import json
    cat = json.load(open("data/stage_catalog.json", encoding="utf-8"))
    assert {k["id"] for k in cat["substage_kinds"]} <= set(messaging._SAMPLE)


def test_custom_test_messages_use_plan_format(monkeypatch):
    sent = []
    monkeypatch.setattr(messaging, "deliver_now",
                        lambda uid, title, body, data=None, kind="message", payload=None: sent.append((kind, title, payload)) or "r")
    messaging.send_test_messages("u1", [
        {"kind": "quiz", "title": "Свой тест", "questions": [
            {"text": "2+2?", "options": [{"text": "4", "correct": True}, {"text": "5"}]}]},
        {"kind": "checklist", "title": "Дела", "checklist": ["Пропуск"]},
        {"kind": "bogus", "title": "", "body": "Текст"}])
    kinds = [k for k, _, _ in sent]
    assert kinds == ["quiz", "checklist", "message"]                 # неизвестный тип -> сообщение
    assert sent[0][2]["questions"][0]["options"][0]["correct"] is True
    assert sent[1][2]["items"][0]["text"] == "Пропуск"
    assert sent[2][1] == "Тестовое сообщение"


def test_sick_leave_notifies_mentor(monkeypatch):
    got = []
    monkeypatch.setattr(messaging, "deliver_now", lambda uid, title, body, **k: got.append((uid, title)) or "r")
    monkeypatch.setattr(messaging.users, "list_users", lambda: [
        {"id": "m1", "full_name": "Петров Пётр"}, {"id": "e1", "full_name": "Иванов Иван"}])
    emp = {"id": "e1", "full_name": "Иванов Иван", "mentor": "петров пётр "}
    assert messaging.notify_mentor_sick(emp, True) and got[0][0] == "m1" and "больничн" in got[0][1]
    assert messaging.notify_mentor_sick(emp, False) and "вернул" in got[1][1]
    assert not messaging.notify_mentor_sick({**emp, "mentor": "Нет Такого"}, True)
