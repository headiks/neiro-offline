"""
Персональное расписание адаптации сотрудника.

Администратор назначает сотруднику готовый план-шаблон и дату выхода на работу
(поля plan_id и start_date в записи пользователя, см. users.py). Расписание
считается на лету:

    план (относительные смещения)
        + дата выхода сотрудника        -> конкретные дата и время каждого сообщения
        + сгенерированные тексты плана  -> содержимое сообщений
        + данные сотрудника             -> подстановка плейсхолдеров [Имя] и т.п.

Тексты под каждого сотрудника заново не генерируются: они зависят от документов
компании, а не от человека. Один сгенерированный план обслуживает сколько угодно
новичков — меняются только даты и плейсхолдеры.
"""

import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import planner
import users


def first_name(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «Иван». Если ФИО из одного слова — берём его."""
    parts = [p for p in (full_name or "").split() if p]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else "коллега"


# Плейсхолдер человека -> (поле сотрудника, роль по падежам: им., род., дат., вин., твор., предл.).
_PERSON = {
    "[ФИО наставника]": ("mentor", ("наставник", "наставника", "наставнику", "наставника", "наставником", "наставнике")),
    "[ФИО руководителя]": ("manager", ("руководитель", "руководителя", "руководителю", "руководителя",
                                       "руководителем", "руководителе")),
}
# Падеж роли по слову перед плейсхолдером: «напишите [ФИО наставника]» -> «напишите наставнику (…)».
_CASE_BY_PREV = (
    (re.compile(r"(?:\bк|напишите|написать|позвоните|позвонить|сообщите|сообщить|скажите|сказать|"
                r"передайте|передать|пишите|звоните)$"), 2),
    (re.compile(r"(?:\bу|\bот|\bдля|\bбез|\bдо|\bнет)$"), 1),
    (re.compile(r"(?:позовите|позвать|спросите|спросить|попросите|попросить|найдите|предупредите|"
                r"предупредить|\bчерез|\bна)$"), 3),
    (re.compile(r"(?:\bс|\bсо)$"), 4),
    (re.compile(r"(?:\bо|\bоб|\bпри)$"), 5),
)


def _person(text: str, placeholder: str, value: str, forms: tuple) -> str:
    """В поле наставника/руководителя бывает должность («Главный администратор»), а не ФИО, и её
    не просклонять. Поэтому роль ставим сами в нужном падеже, а значение — в скобках после неё:
    «начните с наставника (Главный администратор)». Значения нет — остаётся одна роль."""
    tail = f" ({value})" if value else ""
    roles = "|".join(sorted(set(forms), key=len, reverse=True))
    # «наставнику ([ФИО наставника])», «наставника [ФИО наставника]» — роль уже есть
    text = re.sub(rf"(?i)\b({roles})\s*\(?\s*{re.escape(placeholder)}\s*\)?", lambda m: m.group(1) + tail, text)

    def bare(m):
        prev = m.string[max(0, m.start() - 14):m.start()].rstrip().lower()
        case = next((c for rx, c in _CASE_BY_PREV if rx.search(prev)), 0)
        return forms[case] + tail

    return re.sub(re.escape(placeholder), bare, text)


def substitute(text: str, employee: dict) -> str:
    """Подстановка плейсхолдеров, которые генератор оставляет в тексте сообщения."""
    if not text:
        return text
    replacements = {
        "[Имя]": first_name(employee.get("full_name", "")),
        "[ФИО]": employee.get("full_name", ""),
        "[Должность]": employee.get("position", ""),
    }
    if employee.get("mentor"):
        replacements["[ФИО, тел.]"] = employee["mentor"]

    for placeholder, value in replacements.items():
        if value:
            text = text.replace(placeholder, value)
    for placeholder, (field, forms) in _PERSON.items():
        if placeholder in text:
            text = _person(text, placeholder, (employee.get(field) or "").strip(), forms)
    return text


def _substitute_deep(value, employee: dict):
    """Плейсхолдеры во всех строках структуры (пункты чек-листа, вопросы теста)."""
    if isinstance(value, str):
        return substitute(value, employee)
    if isinstance(value, list):
        return [_substitute_deep(v, employee) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_deep(v, employee) for k, v in value.items()}
    return value


def shift_for_pauses(send_at: datetime, pauses: list) -> datetime:
    """Время сообщения с учётом больничных. pauses — [(начало, конец)] по порядку, в том же
    времени, что send_at; у идущего больничного конец — «сейчас». Сообщение, которое пришлось
    бы на больничный или позже него, сдвигается на его длину: план продолжается с того места,
    где сотрудник остановился. Сообщения до больничного (уже полученные) не сдвигаются."""
    for start, end in pauses:
        if send_at >= start:
            send_at += end - start
    return send_at


def local_pauses(employee: dict, tzname: str) -> list:
    """Больничные сотрудника во времени плана (без таймзоны — как send_at расписания)."""
    try:
        tz = ZoneInfo(tzname or planner.DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(planner.DEFAULT_TIMEZONE)
    return [(start.astimezone(tz).replace(tzinfo=None), end.astimezone(tz).replace(tzinfo=None))
            for start, end in users.pause_periods(employee)]


def apply_pauses(items: list, pauses: list) -> list:
    """Сдвигает send_at позиций расписания на больничные (исходное время — в planned_at)."""
    planned = [datetime.fromisoformat(i["schedule"]["send_at"]) for i in items
               if (i.get("schedule") or {}).get("send_at")]
    if not pauses or not planned:
        return items
    # Часы плана идут с первого сообщения: больничный до него ничего не сдвигает.
    first = min(planned)
    pauses = [(max(start, first), end) for start, end in pauses if end > max(start, first)]
    out = []
    for item in items:
        sched = dict(item.get("schedule") or {})
        if sched.get("send_at"):
            planned = datetime.fromisoformat(sched["send_at"])
            shifted = shift_for_pauses(planned, pauses)
            if shifted != planned:
                sched["planned_at"] = sched["send_at"]
                sched["send_at"] = shifted.isoformat(timespec="minutes")
        out.append({**item, "schedule": sched})
    return out


def build_employee_schedule(employee: dict) -> dict:
    """
    Собирает персональное расписание сотрудника.
    Поднимает ValueError, если план не назначен, не найден или нет даты выхода.
    """
    plan_id = employee.get("plan_id")
    if not plan_id:
        raise ValueError("Сотруднику не назначен план адаптации")
    if not employee.get("start_date"):
        raise ValueError("У сотрудника не указана дата выхода на работу")

    plan = planner.load_plan(plan_id)
    if plan is None:
        raise ValueError("Назначенный план не найден — возможно, он удалён")

    # Считаем смещения по дате выхода именно этого сотрудника
    items = planner.resolve_schedule({**plan, "start_date": employee["start_date"]})
    tzname = plan.get("timezone", planner.DEFAULT_TIMEZONE)
    items = apply_pauses(items, local_pauses(employee, tzname))

    # Контент — по «плану профессии» сотрудника, если задан явно; иначе по его должности
    # (расписание этой профессии). Если своего нет — общее (profession="").
    profession = (employee.get("plan_profession") or "").strip() or (employee.get("position") or "")
    schedule = planner.load_schedule(plan_id, profession)
    generated = {m["message_id"]: m for m in (schedule or {}).get("messages", [])}

    messages = []
    for item in items:
        source = generated.get(item["message_id"], {})
        content = dict(source.get("content") or {"format": "markdown", "text": ""})
        content["text"] = substitute(content.get("text", ""), employee)
        if content.get("converted"):
            content["converted"] = _substitute_deep(content["converted"], employee)
        messages.append({
            **item,
            "content": content,
            # В расписании планировщика поле называется topics_used (см. planner.build_schedule);
            # отдаём его под ключом folders_used, который читает админский обзор расписания.
            "folders_used": source.get("topics_used", []),
            "sources": source.get("sources", []),
            "actions": source.get("actions", []),
            "status": source.get("status", "pending"),
            "error": source.get("error"),
        })

    return {
        "schema_version": planner.SCHEMA_VERSION,
        "employee": {
            "id": employee["id"],
            "full_name": employee.get("full_name"),
            "position": employee.get("position"),
            "department": employee.get("department"),
            "contact": employee.get("contact"),
            "phone": employee.get("phone"),
            "email": employee.get("email"),
            "mentor": employee.get("mentor"),
            "manager": employee.get("manager"),
            "status": users.adaptation_status(employee, plan),
        },
        "plan_id": plan_id,
        "plan_title": plan.get("title"),
        "role": plan.get("role"),
        "start_date": employee["start_date"],
        "timezone": tzname,
        # Больничные: план стоит, пока сотрудник болеет, и потом продолжается с того же места.
        "paused": employee.get("status") == "paused",
        "pauses": employee.get("pauses") or [],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "plan_generated": schedule is not None,
        "messages": messages,
    }


