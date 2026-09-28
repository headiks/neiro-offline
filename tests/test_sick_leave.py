"""Больничный: план стоит, пока сотрудник болеет, и после выхода продолжается с того места,
где остановился. Чистая логика сдвига расписания — без БД (сквозной сценарий на реальной
БД — test_api_integration.test_sick_leave_pauses_and_resumes_plan)."""
import test_stubs
test_stubs.install(psycopg=True, stub_modules=())
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import employees  # noqa: E402
import users  # noqa: E402

D = datetime(2026, 10, 1, 9, 0)


def item(send_at):
    return {"message_id": send_at, "schedule": {"send_at": send_at}}


def test_messages_before_pause_stay():
    assert employees.shift_for_pauses(D, [(D + timedelta(days=1), D + timedelta(days=4))]) == D


def test_messages_during_and_after_pause_shift_by_its_length():
    pause = [(D, D + timedelta(days=3))]
    # пришлось бы на больничный -> после выхода, с тем же отступом от начала больничного
    assert employees.shift_for_pauses(D + timedelta(hours=5), pause) == D + timedelta(days=3, hours=5)
    assert employees.shift_for_pauses(D + timedelta(days=10), pause) == D + timedelta(days=13)


def test_several_pauses_add_up():
    pauses = [(D, D + timedelta(days=2)), (D + timedelta(days=5), D + timedelta(days=6))]
    assert employees.shift_for_pauses(D + timedelta(days=1), pauses) == D + timedelta(days=3)
    # после первого больничного сообщение попало во второй — сдвигается и на него
    assert employees.shift_for_pauses(D + timedelta(days=4), pauses) == D + timedelta(days=7)


def test_apply_pauses_keeps_planned_time_and_ignores_pause_before_plan():
    items = [item("2026-10-01T09:00"), item("2026-10-05T09:00")]
    before_plan = [(D - timedelta(days=10), D - timedelta(days=5))]
    assert employees.apply_pauses(items, before_plan) == items          # до плана — ничего не пропущено
    shifted = employees.apply_pauses(items, [(D + timedelta(days=1), D + timedelta(days=3))])
    assert shifted[0]["schedule"] == {"send_at": "2026-10-01T09:00"}
    assert shifted[1]["schedule"] == {"send_at": "2026-10-07T09:00", "planned_at": "2026-10-05T09:00"}
    assert items[1]["schedule"] == {"send_at": "2026-10-05T09:00"}      # исходные позиции не мутируются


def test_pause_periods_and_days():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    user = {"pauses": [{"start": "2026-10-01T12:00:00+00:00", "end": "2026-10-03T12:00:00+00:00"},
                       {"start": "2026-10-09T12:00:00+00:00", "end": None},     # идёт сейчас
                       {"start": "мусор"}]}
    periods = users.pause_periods(user, now)
    assert [(s.day, e.day) for s, e in periods] == [(1, 3), (9, 10)]
    assert users.paused_days(user, now) == 3


def test_open_pause_is_closed():
    closed = users._close_pauses([{"start": "a", "end": "b"}, {"start": "c", "end": None}], "now")
    assert closed == [{"start": "a", "end": "b"}, {"start": "c", "end": "now"}]


def test_sick_days_extend_adaptation():
    plan = {"stages": [{"duration": {"value": 10, "unit": "days"}, "anchor": "from_start"}]}
    user = {"start_date": "2026-10-01", "status": "active",
            "pauses": [{"start": "2026-10-02T09:00:00+00:00", "end": "2026-10-05T09:00:00+00:00"}]}
    assert users.adaptation_status({**user, "pauses": []}, plan, today=date(2026, 10, 12)) == "done"
    assert users.adaptation_status(user, plan, today=date(2026, 10, 12)) == "active"
    assert users.adaptation_status(user, plan, today=date(2026, 10, 14)) == "done"
    assert users.adaptation_status({**user, "status": "paused"}, plan, today=date(2026, 10, 3)) == "paused"
