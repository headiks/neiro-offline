"""
Автоназначение активного общего плана адаптации сотрудникам по должности.

Модель (как и раньше): ОДИН общий план-шаблон задаёт сроки по времени; контент
рассылки берётся под должность сотрудника (см. employees.build_employee_schedule),
а тексты по профессии генерируются из шаблона (plan_schedules). Здесь только выбор
активного общего плана и его автоматическая привязка к сотрудникам.

Правила:
  - активный план хранится в app_settings.default_plan_id;
  - назначается автоматически сотрудникам БЕЗ плана (ручные назначения не трогаем);
  - пока у сотрудника нет даты выхода — расписание не материализуется, план неактивен
    (см. messaging.materialize_employee / employees.build_employee_schedule).
"""
import db
import users

_DEFAULT_KEY = "default_plan_id"


def get_default_plan_id():
    row = db.query("SELECT value FROM app_settings WHERE key = %s", (_DEFAULT_KEY,), "one")
    return (row or {}).get("value") or None


def set_default_plan_id(plan_id):
    """Задать/снять активный общий план. Пустое значение — снять настройку."""
    pid = (plan_id or "").strip()
    if not pid:
        db.execute("DELETE FROM app_settings WHERE key = %s", (_DEFAULT_KEY,))
        return None
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (_DEFAULT_KEY, pid),
    )
    return pid


def default_for_new() -> str:
    """plan_id для нового сотрудника (или '' — если активного плана нет/он удалён).
    Проверяем существование плана, чтобы не привязать к удалённому."""
    pid = get_default_plan_id()
    if not pid:
        return ""
    import planner
    return pid if planner.load_plan(pid) is not None else ""


def assign_unassigned() -> int:
    """Назначить активный общий план сотрудникам БЕЗ плана и материализовать расписания
    тем, у кого уже есть дата выхода. Возвращает число назначенных. Ручные назначения
    (у кого plan_id уже стоит) не трогаются."""
    pid = default_for_new()
    if not pid:
        return 0
    rows = db.query(
        "UPDATE users SET plan_id = %s WHERE role = %s "
        "AND (plan_id IS NULL OR plan_id = '') RETURNING id",
        (pid, users.ROLE_EMPLOYEE), fetch="all",
    ) or []
    if rows:
        try:
            import messaging
            messaging.ensure_all()   # создаст строки инбокса тем, у кого есть дата выхода
        except Exception as e:
            print(f"[autoplan] материализация после автоназначения не выполнена: {e}")
    return len(rows)


