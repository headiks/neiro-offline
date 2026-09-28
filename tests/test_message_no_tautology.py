"""Текст сообщения для сотрудника: пункты, пересказывающие абзац, выбрасываются (тавтология),
новые факты остаются; служебная hr_note сотруднику не показывается."""
import msgconvert

BODY = ("Я — ваш цифровой ассистент и буду рядом все 90 дней адаптации. Помогу с вопросами о работе, "
        "безопасности, оформлении и самочувствии, напомню о важном и передам ваши вопросы наставнику и HR. "
        "Пишите мне в любое время — просто своими словами, например: «Где получить спецодежду?» или «Когда аванс?». "
        "Если ответа у меня не найдётся, я передам вопрос HR, и вам ответят в течение рабочего дня.")
POINTS = ["Я — цифровой ассистент, на связи все 90 дней адаптации.",
          "Помогаю с вопросами о работе, безопасности, оформлении и самочувствии.",
          "Писать можно в любое время и своими словами.",
          "Если не знаю ответа — передам вопрос HR, ответят в течение рабочего дня."]


def test_real_example_has_no_repeats():
    text = msgconvert.to_text({"intro": "Здравствуйте, [Имя]! Рады, что вы с нами.", "body": BODY,
                               "key_points": POINTS, "outro": "Спрашивайте — я здесь, чтобы помочь.",
                               "hr_note": "Уточнить у HR: график работы HR"})
    assert "•" not in text                        # все 4 пункта повторяли абзац
    assert "Уточнить у HR" not in text            # служебная пометка — не сотруднику
    assert text.startswith("Здравствуйте") and text.endswith("чтобы помочь.")


def test_new_facts_kept():
    text = msgconvert.to_text({"body": "Пропуск выдают на проходной №1.",
                               "key_points": ["Пропуск выдают на проходной №1", "С собой: паспорт, СНИЛС, фото 3×4"]})
    assert text.count("•") == 1 and "паспорт, СНИЛС" in text


def test_rerender_saved_texts(monkeypatch):
    import sys, types
    sys.modules.setdefault("indexing", types.SimpleNamespace())
    import planner
    sch = {"messages": [
        {"status": "generated", "substage": {"kind": "message"},
         "content": {"format": "unified/1", "body": BODY, "key_points": POINTS, "hr_note": "Уточнить у HR: x",
                     "text": "старый текст с повторами", "converted": {}}},
        {"status": "edited", "substage": {"kind": "message"},
         "content": {"format": "unified/1", "body": "Текст админа.", "key_points": ["старый пункт"],
                     "text": "Текст админа.", "converted": {}}}]}
    saved, refreshed = [], []
    monkeypatch.setattr(planner, "list_plans", lambda: [{"plan_id": "p1"}])
    monkeypatch.setattr(planner, "list_schedule_professions", lambda pid: [])
    monkeypatch.setattr(planner, "_load_exact", lambda pid, prof: sch if prof == "" else None)
    monkeypatch.setattr(planner, "save_schedule", lambda pid, s, profession="": saved.append(pid))
    monkeypatch.setattr(planner, "_refresh_inboxes", lambda pid: refreshed.append(pid))
    assert planner.rerender_saved_texts() == 2 and saved == ["p1"] and refreshed == ["p1"]
    gen, edited = (m["content"] for m in sch["messages"])
    assert "•" not in gen["text"] and "Уточнить у HR" not in gen["text"]
    assert edited["text"] == "Текст админа." and edited["converted"]["text"] == "Текст админа."
    assert planner.rerender_saved_texts() == 0                 # повторно — ничего не меняет
