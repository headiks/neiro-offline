"""
Доставка сообщений плана адаптации по расписанию.

Расписание считается на лету (employees.build_employee_schedule), но чтобы доставлять
сообщения в нужный момент и не слать дважды, каждое сообщение материализуется строкой в
таблице scheduled_messages (см. db.py). Эта же таблица — инбокс сотрудника.

Жизненный цикл сообщения:
    pending  — материализовано, время ещё не наступило
    delivered — планировщик выпустил его в кабинет (send_at <= now)
    read     — сотрудник открыл
Доставка = смена pending -> delivered (без внешних каналов; сотрудник видит его в кабинете).

Планировщик: фоновый цикл в приложении (start_scheduler) ИЛИ разовый прогон из CLI
(dispatch_messages.py). Оба зовут dispatch_all(): по каждой схеме (public + все кабинеты
из public.cabinets) — досоздать недостающие строки и выпустить наступившие.

ponytail: время сообщения берётся в таймзоне плана (одна на план); индивидуальная
таймзона сотрудника не моделируется — добавить поле, если понадобится.
"""
import os
import time
import threading
import contextlib
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

import db
import users
import employees as adaptation
from planner import DEFAULT_TIMEZONE

_started = False


def _localize(iso_naive: str, tzname: str) -> datetime:
    """Наивное локальное время плана ('2026-09-10T09:00') -> абсолютный момент с таймзоной."""
    dt = datetime.fromisoformat(iso_naive)
    try:
        tz = ZoneInfo(tzname or DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return dt.replace(tzinfo=tz)


def _message_row(employee_id: str, plan_id, msg: dict, tzname: str):
    """Готовит поля строки scheduled_messages из позиции расписания. Чистая (тестируется).
    None — если нет даты или сообщение ещё не сгенерировано: пустой «Этап — Подэтап» без
    текста сотруднику не шлём; появится текст — refresh_plan досоздаст строку."""
    send_iso = (msg.get("schedule") or {}).get("send_at")
    if not send_iso:
        return None
    content = msg.get("content") or {}
    body = content.get("text", "") or ""
    if not body.strip():
        return None
    stage = msg.get("stage") or {}
    sub = msg.get("substage") or {}
    title = " — ".join(p for p in (stage.get("title"), sub.get("title")) if p)
    return {
        "id": f"{employee_id}:{msg['message_id']}",
        "employee_id": employee_id,
        "plan_id": plan_id,
        "message_id": msg["message_id"],
        "stage_id": stage.get("id"),
        "substage_id": sub.get("id"),
        "title": title,
        "body": body,
        "kind": sub.get("kind") or content.get("kind") or "message",
        # Структура под тип (пункты чек-листа, вопросы теста/опроса) — для показа в
        # приложении и кабинете. Без неё — просто текст (body).
        "payload": content.get("converted"),
        "send_at": _localize(send_iso, tzname),
    }


# ---------- Материализация ----------
def materialize_employee(employee: dict, force: bool = False) -> int:
    """Создаёт/обновляет строки расписания для сотрудника. Возвращает число строк.
    force=True пересобирает будущие (pending) строки, доставленные/прочитанные не трогает."""
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError:
        return 0   # плана или даты выхода нет — материализовать нечего

    tzname = schedule.get("timezone") or DEFAULT_TIMEZONE
    plan_id = schedule.get("plan_id")
    rows = [r for r in (_message_row(employee["id"], plan_id, m, tzname)
                        for m in schedule.get("messages", [])) if r]

    if force:
        # Только строки плана: отложенные тестовые/служебные уведомления (plan_id NULL) не трогаем.
        db.execute("DELETE FROM scheduled_messages WHERE employee_id = %s AND status = 'pending' "
                   "AND plan_id IS NOT NULL", (employee["id"],))
    if not rows:
        return 0
    for r in rows:
        # Обновляем только ещё не доставленные строки — историю не переписываем.
        db.execute(
            "INSERT INTO scheduled_messages "
            "(id, employee_id, plan_id, message_id, stage_id, substage_id, title, body, kind, payload, send_at) "
            "VALUES (%(id)s, %(employee_id)s, %(plan_id)s, %(message_id)s, %(stage_id)s, "
            "%(substage_id)s, %(title)s, %(body)s, %(kind)s, %(payload)s, %(send_at)s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  title = EXCLUDED.title, body = EXCLUDED.body, send_at = EXCLUDED.send_at, "
            "  kind = EXCLUDED.kind, payload = EXCLUDED.payload, "
            "  plan_id = EXCLUDED.plan_id, stage_id = EXCLUDED.stage_id, "
            "  substage_id = EXCLUDED.substage_id, updated_at = now() "
            "WHERE scheduled_messages.status = 'pending'",
            {**r, "payload": Json(r["payload"]) if r.get("payload") else None},
        )
    return len(rows)


def ensure_all() -> int:
    """Досоздаёт строки плана для сотрудников с планом и датой выхода, у которых строк
    ЭТОГО плана ещё нет. Считаем только строки плана (plan_id): раньше любая строка —
    тестовое уведомление или ответ на вопрос — навсегда блокировала рассылку плана."""
    # Отбор в SQL: планировщик ходит сюда каждую минуту, а полный список пользователей
    # (с расшифровкой ПДн) на тысячах сотрудников — лишняя работа на каждом тике.
    rows = db.query(
        "SELECT * FROM users u WHERE u.role = %s AND COALESCE(u.plan_id, '') <> '' "
        "AND COALESCE(u.start_date, '') <> '' AND NOT EXISTS (SELECT 1 FROM scheduled_messages s "
        "WHERE s.employee_id = u.id AND s.plan_id = u.plan_id)", (users.ROLE_EMPLOYEE,)) or []
    made = 0
    for row in rows:
        made += materialize_employee(users.from_row(row))
    return made


def refresh_plan(plan_id: str) -> int:
    """Тексты плана изменились (генерация, правка) -> пересобрать ещё не доставленные
    сообщения у всех сотрудников с этим планом. Без этого сотрудники, заведённые до
    генерации, получали бы пустые сообщения. -> число сотрудников."""
    rows = db.query("SELECT * FROM users WHERE role = %s AND plan_id = %s AND COALESCE(start_date, '') <> ''",
                    (users.ROLE_EMPLOYEE, plan_id)) or []
    for row in rows:
        materialize_employee(users.from_row(row), force=True)
    return len(rows)


# ---------- Доставка ----------
def dispatch_due() -> int:
    """Выпускает в кабинет все наступившие сообщения текущей схемы. Возвращает число.
    После доставки шлёт push на устройства сотрудника (best-effort: сбой пуша не влияет
    на инбокс — он источник правды)."""
    rows = db.query(
        "UPDATE scheduled_messages SET status = 'delivered', delivered_at = now(), "
        "updated_at = now() WHERE status = 'pending' AND send_at <= now() "
        # Сотрудник на больничном (status='paused') не получает сообщения плана. После
        # выхода set_sick сдвигает их на срок болезни — план продолжится с того же места.
        "AND employee_id NOT IN (SELECT id FROM users WHERE status = 'paused') "
        "RETURNING id, employee_id, title, body, kind",
        fetch="all",
    )
    rows = rows or []
    if rows:
        try:
            import push
            push.notify(group_pushes(rows))
        except Exception as e:
            print(f"[scheduler] push не отправлен: {e}")
    return len(rows)


def group_pushes(rows: list) -> list:
    """Одно уведомление на сотрудника за проход: несколько сообщений, наступивших разом
    (например, «сессия» дня), приходят одним пушем «N новых сообщений», а не россыпью."""
    by_user: dict = {}
    for r in rows:
        by_user.setdefault(r["employee_id"], []).append(r)
    out = []
    for uid, items in by_user.items():
        if len(items) == 1:
            r = items[0]
            out.append({"user_id": uid, "title": r["title"] or "НейроМастер",
                        "body": push_body(r.get("kind"), r["body"]),
                        "data": {"message_row_id": r["id"], "kind": r.get("kind") or "message"}})
            continue
        titles = [r["title"] for r in items if r.get("title")]
        n = len(items)
        word = "новое сообщение" if n % 10 == 1 and n % 100 != 11 else (
            "новых сообщения" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "новых сообщений")
        out.append({"user_id": uid, "title": f"НейроМастер: {n} {word}",
                    "body": "\n".join(f"• {t}" for t in titles[:5]) + ("\n…" if len(titles) > 5 else ""),
                    "data": {"message_row_id": items[0]["id"], "kind": "batch", "count": str(n)}})
    return out


def _schemas():
    """Схемы для обработки: public (None) + все кабинеты из реестра."""
    yield None
    try:
        for r in db.query("SELECT schema_name FROM public.cabinets"):
            yield r["schema_name"]
    except Exception:
        pass   # реестра кабинетов ещё нет — работаем только с public


def dispatch_all() -> int:
    """Один проход по всем схемам: досоздать недостающее и выпустить наступившее."""
    total = 0
    for schema in _schemas():
        ctx = db.use_schema(schema) if schema else contextlib.nullcontext()
        with ctx:
            try:
                ensure_all()
                total += dispatch_due()
            except Exception as e:
                print(f"[scheduler] схема {schema or 'public'}: {e}")
    return total


KIND_PUSH_HINT = {
    "checklist": "Чек-лист — отметьте выполненное в приложении.",
    "system_check": "Проверка — отметьте выполненное в приложении.",
    "survey": "Короткий опрос — ответьте в приложении.",
    "quiz": "Мини-тест — ответьте в приложении.",
}


def push_body(kind: str, body: str) -> str:
    """Текст пуша: у интерактивных типов — подсказка, что ответить нужно в приложении."""
    hint = KIND_PUSH_HINT.get(kind or "")
    text = (body or "").strip()
    return f"{hint}\n{text}" if hint else text


# ---------- Инбокс сотрудника ----------
def inbox(employee_id: str, limit: int = 200) -> list:
    return db.query(
        "SELECT id, message_id, title, body, kind, payload, answers, send_at, status, "
        "delivered_at, read_at, stage_id, substage_id FROM scheduled_messages "
        "WHERE employee_id = %s AND status IN ('delivered', 'read') "
        "ORDER BY send_at DESC LIMIT %s",
        (employee_id, max(1, min(int(limit), 500))),
    )


def unread_count(employee_id: str) -> int:
    r = db.query("SELECT count(*) AS n FROM scheduled_messages "
                 "WHERE employee_id = %s AND status = 'delivered'", (employee_id,), "one")
    return r["n"] if r else 0


def deliver_now(user_id: str, title: str = "", body: str = "", data: dict | None = None,
                kind: str = "message", payload: dict | None = None) -> str:
    """Кладёт сообщение сразу в инбокс сотрудника (delivered) и шлёт push.
    Для уведомлений вне плана: ответ на вопрос, тест уведомлений. Сообщение видно
    в приложении (вкладка «Сегодня») даже без настроенного push. Возвращает id строки."""
    import uuid
    mid = f"note-{uuid.uuid4().hex[:8]}"
    row_id = f"{user_id}:{mid}"
    title = title or "Уведомление"
    db.execute(
        "INSERT INTO scheduled_messages "
        "(id, employee_id, message_id, title, body, kind, payload, send_at, status, delivered_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), 'delivered', now())",
        (row_id, user_id, mid, title, body or "", kind or "message", Json(payload) if payload else None),
    )
    try:
        import push
        push.notify([{"user_id": user_id, "title": title, "body": push_body(kind, body),
                      "data": {**(data or {}), "message_row_id": row_id, "kind": kind or "message"}}])
    except Exception as e:
        print(f"[deliver_now] push не отправлен: {e}")
    return row_id


def push_test(employee_id: str, title: str = "", body: str = "",
              delay_seconds: int = 0) -> str:
    """Кладёт тестовое сообщение в инбокс для ручной проверки уведомлений из админки.
    delay_seconds<=0 — сразу delivered. delay_seconds>0 — pending с send_at в будущем,
    выпустит фоновый планировщик (точность ~ его интервал, NEIROMASTER_SCHEDULER_INTERVAL,
    по умолчанию 60 с). Возвращает id созданной строки."""
    import uuid
    mid = f"test-{uuid.uuid4().hex[:8]}"
    row_id = f"{employee_id}:{mid}"
    title = title or "Тестовое уведомление"
    body = body or "Проверка системы уведомлений НейроМастер."
    delay = max(0, int(delay_seconds or 0))
    if delay <= 0:
        return deliver_now(employee_id, title, body)
    else:
        db.execute(
            "INSERT INTO scheduled_messages "
            "(id, employee_id, message_id, title, body, send_at, status) "
            "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s), 'pending')",
            (row_id, employee_id, mid, title, body, delay),
        )
    return row_id


def mark_read(employee_id: str, message_row_id: str) -> bool:
    """message_row_id — id строки инбокса. Принимаем и голый message_id: так отмечали
    уже установленные версии приложения (строка = <сотрудник>:<message_id>)."""
    rows = db.query(
        "UPDATE scheduled_messages SET status = 'read', read_at = now(), updated_at = now() "
        "WHERE (id = %s OR id = %s) AND employee_id = %s AND status = 'delivered' RETURNING id",
        (message_row_id, f"{employee_id}:{message_row_id}", employee_id), fetch="all",
    )
    return bool(rows)


def save_answers(employee_id: str, message_row_id: str, answers: dict) -> bool:
    """Ответы сотрудника на чек-лист/опрос/тест ({id пункта/вопроса: значение}).
    Хранятся в строке инбокса — видны и в приложении, и в кабинете на сайте."""
    rows = db.query(
        "UPDATE scheduled_messages SET answers = %s, "
        "status = CASE WHEN status = 'delivered' THEN 'read' ELSE status END, "
        "read_at = COALESCE(read_at, now()), updated_at = now() "
        "WHERE id = %s AND employee_id = %s AND status IN ('delivered', 'read') RETURNING id",
        (Json(answers or {}), message_row_id, employee_id), fetch="all",
    )
    return bool(rows)


# ---------- Больничный ----------
def set_sick(employee_id: str, sick: bool) -> dict:
    """Больничный: план полностью встаёт, после выхода продолжается с того места, где
    сотрудник остановился (будущие сообщения сдвигаются на срок болезни, пропущенное не
    приходит пачкой). Ставит сам сотрудник («Я на больничном») или администратор."""
    before = users.get_user(employee_id)
    if not before:
        raise ValueError("Пользователь не найден")
    if (before.get("status") == "paused") == sick:
        return before
    if sick:
        user = users.set_status(employee_id, "paused")
    else:
        # Порядок важен: сначала закрыть больничный и сдвинуть ожидающие сообщения, и только
        # потом снять паузу. Иначе планировщик успел бы выпустить их по старым датам разом.
        user = users.close_pause(employee_id)
        materialize_employee(user, force=True)
        user = users.set_status(employee_id, "active")
    notify_mentor_sick(user, sick)
    return user


def notify_mentor_sick(employee: dict, sick: bool) -> bool:
    """Сотрудник ушёл на больничный / вернулся -> уведомление его наставнику (инбокс + пуш).
    Наставник в карточке — ФИО; ищем пользователя с таким ФИО. Не нашли -> False."""
    name = (employee.get("mentor") or "").strip().lower()
    if not name:
        return False
    mentor = next((u for u in users.list_users()
                   if (u.get("full_name") or "").strip().lower() == name and u["id"] != employee["id"]), None)
    if mentor is None:
        return False
    who = employee.get("full_name") or employee.get("username") or "Сотрудник"
    if sick:
        deliver_now(mentor["id"], f"{who} на больничном",
                    f"{who} отметил(а) больничный. План адаптации поставлен на паузу до выхода.")
    else:
        deliver_now(mentor["id"], f"{who} вернулся(ась) к работе",
                    f"{who} снял(а) отметку о больничном. План адаптации продолжится с того места, где остановился.")
    return True


# ---------- Тест: по сообщению каждого типа ----------
_SAMPLE = {
    "message": ("Добро пожаловать!", {
        "intro": "Здравствуйте! Рады, что вы с нами.",
        "body": "Завтра ваш первый рабочий день. Ждём вас к 9:00 на проходной, пропуск выдаст охрана.",
        "key_points": ["Возьмите паспорт", "Обед — с 12:00 до 13:00"],
        "outro": "Если будут вопросы — пишите во вкладку «Вопрос»."}),
    "reminder": ("Напоминание", {
        "body": "Сегодня в 14:00 — вводный инструктаж по охране труда, кабинет 205."}),
    "checklist": ("Чек-лист первого дня", {
        "intro": "Отметьте, что уже сделано:",
        "checklist": ["Получить пропуск", "Получить спецодежду", "Пройти вводный инструктаж",
                      "Познакомиться с наставником"]}),
    "system_check": ("Проверка доступов", {
        "intro": "Проверьте, что всё работает:",
        "checklist": ["Вход в рабочую почту", "Доступ к порталу", "Работает пропуск"]}),
    "survey": ("Как прошла первая неделя?", {
        "intro": "Ответьте на пару вопросов — это поможет сделать адаптацию лучше.",
        "questions": [
            {"text": "Насколько понятны ваши задачи?", "options": ["Всё понятно", "Частично", "Непонятно"]},
            {"text": "Хватает ли помощи наставника?", "options": ["Да", "Скорее да", "Нет"]}]}),
    "quiz": ("Мини-тест: охрана труда", {
        "intro": "Проверим, что запомнилось после инструктажа.",
        "questions": [
            {"text": "Что делать при пожаре в первую очередь?",
             "options": [{"text": "Сообщить по телефону 112 и начальнику", "correct": True},
                         {"text": "Закончить работу"}, {"text": "Открыть окна"}],
             "explanation": "Сначала — сообщить о пожаре, затем эвакуация по плану."},
            {"text": "Где хранится аптечка первой помощи?",
             "options": [{"text": "У мастера участка", "correct": True}, {"text": "В столовой"}],
             "explanation": "Аптечка — у мастера участка, место отмечено на плане эвакуации."}]}),
    "handover": ("Передача наставнику", {
        "body": "С завтрашнего дня вас сопровождает наставник — он подойдёт к вам в 9:00."}),
}


TEST_FIELDS = ("intro", "body", "checklist", "questions")


def test_samples() -> list:
    """Примеры тестовых сообщений всех типов в формате редактора админки:
    {kind, title, intro, body, checklist[], questions[{text, options[{text, correct}], explanation}]}.
    У текстовых типов всё содержимое сведено в body — его админ и правит."""
    import msgconvert
    out = []
    for kind, (title, raw) in _SAMPLE.items():
        item = {"kind": kind, "title": title, "intro": raw.get("intro", ""), "body": raw.get("body", ""),
                "checklist": list(raw.get("checklist") or []), "questions": [], "delay": 0}
        if msgconvert.KIND_TO_FORMAT.get(kind) == "message":
            item["intro"], item["body"] = "", msgconvert.to_text({"title": "", **raw})
        for q in raw.get("questions") or []:
            item["questions"].append({
                "text": q["text"], "explanation": q.get("explanation", ""),
                "options": [o if isinstance(o, dict) else {"text": o} for o in q.get("options") or []]})
        out.append(item)
    return out


def send_test_messages(user_id: str, items: list | None = None) -> list:
    """Тестовые сообщения из админки (страница /message-test): любые типы и содержимое,
    в том же формате, что сообщения плана (msgconvert), — чтобы проверить, как они выглядят
    и приходят в приложение. delay>0 — выпустит планировщик через delay секунд.
    items=None — по одному примеру каждого типа."""
    import uuid
    import msgconvert
    out = []
    for it in (items if items is not None else test_samples()):
        kind = it.get("kind") if it.get("kind") in msgconvert.KIND_TO_FORMAT else "message"
        title = (it.get("title") or "").strip() or "Тестовое сообщение"
        content = {"title": title, **{k: it.get(k) for k in TEST_FIELDS if it.get(k)}}
        payload = msgconvert.convert(content, kind)
        text = msgconvert.to_text(content)
        delay = max(0, int(it.get("delay") or 0))
        if delay <= 0:
            out.append(deliver_now(user_id, title, text, kind=kind, payload=payload))
            continue
        mid = f"test-{uuid.uuid4().hex[:8]}"
        row_id = f"{user_id}:{mid}"
        db.execute(
            "INSERT INTO scheduled_messages "
            "(id, employee_id, message_id, title, body, kind, payload, send_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s), 'pending')",
            (row_id, user_id, mid, title, text, kind, Json(payload), delay),
        )
        out.append(row_id)
    return out


# ---------- Планировщик (фоновый цикл) ----------
def _loop(interval: int):
    # При нескольких процессах (web-воркеры + worker'ы) доставку должен вести ТОЛЬКО
    # один — иначе сообщения плана уйдут по несколько раз. Выбираем лидера через
    # Redis-ключ с TTL: держатель продлевает его каждый тик, при его смерти ключ
    # протухает и лидерство перехватывает следующий процесс. Без Redis — процесс один.
    from redis_conn import get_redis
    lock_key = "nm:leader:scheduler"
    ttl = interval * 3
    me = str(os.getpid())
    while True:
        try:
            leader = True
            r = get_redis()
            if r is not None:
                cur = r.get(lock_key)
                if cur == me:
                    r.expire(lock_key, ttl)               # мы лидер — продлеваем
                elif cur is None:
                    leader = bool(r.set(lock_key, me, nx=True, ex=ttl))  # берём лидерство
                else:
                    leader = False                        # лидер другой — пропускаем проход
            if leader:
                dispatch_all()
        except Exception as e:
            print(f"[scheduler] сбой прохода: {e}")
        time.sleep(interval)


def start_scheduler():
    """Запуск фонового цикла доставки. Отключается NEIROMASTER_SCHEDULER=0.
    Интервал опроса — NEIROMASTER_SCHEDULER_INTERVAL секунд (по умолчанию 60)."""
    global _started
    if _started or os.environ.get("NEIROMASTER_SCHEDULER", "1").lower() in ("0", "false", "no"):
        return
    _started = True
    interval = max(5, int(os.environ.get("NEIROMASTER_SCHEDULER_INTERVAL", "60")))
    threading.Thread(target=_loop, args=(interval,), daemon=True).start()
    print(f"[scheduler] фоновый цикл доставки запущен (каждые {interval} с)")


if __name__ == "__main__":
    msg = {
        "message_id": "s1.a", "schedule": {"send_at": "2026-09-10T09:00"},
        "stage": {"id": "s1", "title": "Вводный"},
        "substage": {"id": "a", "title": "Знакомство"},
        "content": {"text": "Привет!"},
    }
    row = _message_row("emp1", "plan1", msg, "Europe/Moscow")
    assert row["id"] == "emp1:s1.a", row
    assert row["title"] == "Вводный — Знакомство"
    assert row["body"] == "Привет!"
    assert row["send_at"].utcoffset() is not None            # локализовано (aware)
    assert row["send_at"].hour == 9 and row["stage_id"] == "s1"
    assert _message_row("e", "p", {"message_id": "x", "schedule": {}}, "UTC") is None  # без send_at
    print("messaging: _message_row/_localize — OK")
