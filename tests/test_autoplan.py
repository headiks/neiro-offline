"""
Проверка автоназначения активного общего плана без Postgres.
Заглушки db/psycopg ставятся ДО импорта autoplan (см. test_stubs).
Запуск: python test_autoplan.py
"""
import sys
import types
import test_stubs

test_stubs.install()   # db (пустышка) + config

import autoplan

# In-memory состояние + управляемый db-стаб.
_store = {"settings": {}, "users": [
    {"id": "u1", "role": "employee", "plan_id": None},
    {"id": "u2", "role": "employee", "plan_id": "manual"},   # ручной — не трогать
    {"id": "a1", "role": "admin", "plan_id": None},          # админ — не трогать
]}


def _query(sql, params=(), fetch="all"):
    s = sql.lower()
    if "from app_settings" in s:
        v = _store["settings"].get("default_plan_id")
        return {"value": v} if v else None
    if s.startswith("update users"):
        pid, role = params
        hit = [u for u in _store["users"] if u["role"] == role and not u.get("plan_id")]
        for u in hit:
            u["plan_id"] = pid
        return [{"id": u["id"]} for u in hit]
    return None


def _execute(sql, params=()):
    s = sql.lower()
    if "delete from app_settings" in s:
        _store["settings"].pop("default_plan_id", None)
    elif "insert into app_settings" in s:
        _store["settings"]["default_plan_id"] = params[1]


sys.modules["db"].query = _query
sys.modules["db"].execute = _execute
sys.modules["planner"] = types.SimpleNamespace(load_plan=lambda pid: {"id": pid})
sys.modules["messaging"] = types.SimpleNamespace(ensure_all=lambda: 0)


def test_default_get_set():
    autoplan.set_default_plan_id("")               # не зависим от порядка тестов
    assert autoplan.get_default_plan_id() is None
    autoplan.set_default_plan_id("planA")
    assert autoplan.get_default_plan_id() == "planA"
    autoplan.set_default_plan_id("")
    assert autoplan.get_default_plan_id() is None


def test_assign_only_unassigned():
    autoplan.set_default_plan_id("planA")
    n = autoplan.assign_unassigned()
    assert n == 1, n                                   # только u1
    assert _store["users"][0]["plan_id"] == "planA"
    assert _store["users"][1]["plan_id"] == "manual"   # ручной не тронут
    assert _store["users"][2]["plan_id"] is None       # админ не тронут
    assert autoplan.assign_unassigned() == 0           # больше некого


def test_no_default_no_assign():
    autoplan.set_default_plan_id("")
    assert autoplan.assign_unassigned() == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("OK ", name)
    print("test_autoplan: все проверки пройдены")
