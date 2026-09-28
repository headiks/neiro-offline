"""Люди: импорт штатного расписания, профили, роли, назначение планов адаптации."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
import json
from urllib.parse import quote

from pydantic import BaseModel, Field

import auth
import users
import staffing
import planner
import employees as adaptation
import messaging
import activitylog
import rawdb
from config import MAX_UPLOAD_BYTES
from deps import require_admin, require_owner, admin_only, owner_only, globaltest_only

router = APIRouter()


# ---------- Штатное расписание (первичный инструмент: люди и должности) ----------
class StaffingImportRequest(BaseModel):
    records: list = Field(default_factory=list, max_length=5000)


def _foreign_department(actor: dict, department: str) -> bool:
    """Обычный администратор заводит людей только своего подразделения: иначе он создал бы
    сотрудников, которых сам не видит и не может править (список фильтруется по отделу)."""
    if users.is_owner(actor):
        return False
    own = (actor.get("department") or "").strip().lower()
    dep = (department or "").strip().lower()
    return bool(own and dep and dep != own)


@router.post("/staffing/preview")
def staffing_preview(file: UploadFile = File(...), actor: dict = Depends(require_admin)):
    """Разбор загруженной xlsx-штатки: ИИ определяет разметку столбцов, возвращаем
    найденное сопоставление и извлечённые записи для подтверждения администратором."""
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    rawdb.put_staffing(file.filename, content, actor.get("id"))   # исходник — в raw-БД
    try:
        result = staffing.parse_file(content, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось разобрать таблицу: {e}")
    # Уже заведённых помечаем сразу: админ видит до импорта, что они будут пропущены.
    known = staffing.existing_keys()
    for r in result.get("records") or []:
        r["exists"] = staffing.record_key(r) in known
        r["foreign"] = _foreign_department(actor, r.get("department"))
    result["count"] = len(result.get("records") or [])
    return result


@router.post("/staffing/import")
def staffing_import(req: StaffingImportRequest, actor: dict = Depends(require_admin)):
    """Массовое создание из подтверждённых строк единой таблицы: строки с ФИО — профили
    сотрудников (логины/пароли в ответе, один раз), строки без ФИО — профили-вакансии.
    Обычный администратор создаёт только людей своего подразделения (пустое — его)."""
    records = [r for r in (req.records or []) if isinstance(r, dict)][:5000]
    foreign = []
    if not users.is_owner(actor):
        own = actor.get("department") or ""
        mine = []
        for r in records:
            if _foreign_department(actor, r.get("department")):
                foreign.append({"full_name": r.get("full_name") or r.get("position") or "—",
                                "reason": "другое подразделение — заводит суперадмин"})
            else:
                mine.append({**r, "department": (r.get("department") or "").strip() or own})
        records = mine
    result = staffing.import_records(records, actor=actor)
    result["skipped"] = foreign + result.get("skipped", [])
    activitylog.log("action", user=actor, path="/staffing/import",
                    detail={"action": "staffing_import", "profiles": len(result.get("profiles", [])),
                            "vacancies": len(result.get("vacancies", []))})
    return result


class IdsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=5000)


def _delete_denied(actor: dict, target: dict) -> str:
    """Причина, по которой актор не может удалить пользователя ('' — может). Одно правило
    для одиночного и массового удаления: себя нельзя; обычный администратор — только
    сотрудников своего подразделения; суперадмин — любого (последнего owner не даст users)."""
    if target.get("id") == actor.get("id"):
        return "нельзя удалить себя"
    if not users.is_owner(actor) and (target.get("role") != users.ROLE_EMPLOYEE
                                      or not users.can_manage(actor, target)):
        return "недостаточно прав"
    return ""


@router.post("/users/bulk-delete")
def bulk_delete(req: IdsRequest, actor: dict = Depends(require_admin)):
    """Удаление отмеченных галочками. Каждого проверяем как одиночное удаление: себя нельзя,
    обычный админ — только сотрудников своего отдела. Неподходящие — в skipped с причиной."""
    deleted, skipped = [], []
    for uid in dict.fromkeys(req.ids):
        target = users.get_user(uid)
        if target is None:
            continue
        name = target.get("full_name") or uid
        reason = _delete_denied(actor, target)
        if reason:
            skipped.append({"full_name": name, "reason": reason})
            continue
        try:
            if users.delete_user(uid):
                auth.drop_user_sessions(uid)
                deleted.append(uid)
        except ValueError as e:
            skipped.append({"full_name": name, "reason": str(e)})
    activitylog.log("action", user=actor, path="/users/bulk-delete",
                    detail={"action": "users_bulk_delete", "deleted": len(deleted)})
    return {"deleted": len(deleted), "skipped": skipped}


@router.get("/users/credentials.xlsx")
def credentials_xlsx(ids: str = "", actor: dict = Depends(require_admin)):
    """Excel «ФИО / логин / временный пароль» — по сотрудникам, ещё не задавшим свой пароль.
    ids (через запятую) — только эти (сразу после загрузки штатки); пусто — все видимые."""
    wanted = {i for i in ids.split(",")[:5000] if i}
    rows = [u for u in users.visible_users(actor, users.list_users(with_secrets=True))
            if u.get("temp_password")
            and (not wanted or u["id"] in wanted)]
    body = staffing.credentials_xlsx(rows)
    disposition = ('attachment; filename="logins.xlsx"; '
                   "filename*=UTF-8''" + quote("логины_и_пароли.xlsx"))
    return Response(content=body,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": disposition})


# ---------- Пользователи: профили, роли, назначение планов ----------
class UserRequest(BaseModel):
    full_name: str
    username: str | None = None
    password: str | None = None
    position: str | None = None
    department: str | None = None
    contact: str | None = None
    phone: str | None = None
    email: str | None = None
    mentor: str | None = None
    manager: str | None = None
    plan_id: str | None = None
    plan_profession: str | None = None
    start_date: str | None = None
    status: str | None = None
    notes: str | None = None


class RoleRequest(BaseModel):
    role: str


class ActiveRequest(BaseModel):
    active: bool


class PauseRequest(BaseModel):
    paused: bool


class TargetCredentialsRequest(BaseModel):
    username: str | None = None
    password: str | None = None


def _target_user(user_id: str, actor: dict) -> dict:
    """Находит пользователя и проверяет, что актор вправе его менять."""
    target = users.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        users.ensure_can_manage(actor, target)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return target


@router.get("/users")
def get_users(actor: dict = Depends(require_admin)):
    """Суперадмин видит всех, администратор — только людей своего отдела (и себя)."""
    plans = {p["plan_id"]: p for p in planner.list_plans()}
    full = {}                                   # полные планы — для длительности (автостатус)
    result = []
    for user in users.visible_users(actor, users.list_users(with_secrets=True)):
        plan = plans.get(user.get("plan_id"))
        pid = user.get("plan_id")
        if pid and pid not in full:
            full[pid] = planner.load_plan(pid)
        result.append({
            **user,
            "status": users.adaptation_status(user, full.get(pid)),
            "plan_title": plan["title"] if plan else None,
            "plan_generated": bool(plan and plan.get("generated")),
            "has_account": bool(user.get("username")),
        })
    return {"users": result, "scope": "all" if users.is_owner(actor) else "department"}


@router.post("/users", dependencies=admin_only)
def create_user(req: UserRequest, actor: dict = Depends(require_admin)):
    """
    Заведение сотрудника администратором. Логин и временный пароль необязательны:
    профиль можно создать заранее, а доступ выдать позже.
    """
    payload = req.model_dump()
    # Логин выдаётся один раз из фамилии и инициалов и не редактируется; временный пароль —
    # случайный, админ видит его в списке и в Excel до первого входа сотрудника.
    payload["username"] = staffing.new_username(payload.get("full_name") or "")
    payload["password"] = staffing.temp_password()
    # Обычный администратор заводит людей только в своё подразделение: иначе он создал бы
    # человека, которого сам же не увидит (список людей отфильтрован по отделу).
    if not users.is_owner(actor) and actor.get("department"):
        payload["department"] = actor["department"]
    try:
        user = users.create_user(payload, actor=actor, role=users.ROLE_EMPLOYEE,
                                 issued_password=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**users.public_view(user), "temp_password": payload["password"]}


@router.get("/users/{user_id}")
def get_user(user_id: str, actor: dict = Depends(require_admin)):
    return users.public_view(_target_user(user_id, actor))


@router.put("/users/{user_id}")
def update_user(user_id: str, req: UserRequest, actor: dict = Depends(require_admin)):
    """Правка профиля и назначение плана адаптации с датой выхода."""
    before = _target_user(user_id, actor)
    payload = req.model_dump()
    # Подразделение определяет, кого видит администратор. Обычный админ не меняет его ни
    # себе (иначе «переехал» бы в чужой отдел и увидел его людей), ни сотрудникам.
    if not users.is_owner(actor):
        payload["department"] = before.get("department") or ""
    user = users.update_profile(user_id, payload)
    # Сменились план/дата выхода/должность -> будущие сообщения плана пересобираются сразу
    # (иначе сотрудник получал бы сообщения старого плана или не получал вовсе).
    keys = ("plan_id", "start_date", "plan_profession", "position")
    if any((before.get(k) or "") != (user.get(k) or "") for k in keys):
        messaging.materialize_employee(user, force=True)
    return users.public_view(user)


@router.delete("/users/{user_id}", dependencies=admin_only)
def remove_user(user_id: str, actor: dict = Depends(require_admin)):
    """Удаление пользователя: обычный админ — только сотрудников,
    суперадмин — любого (кроме самого себя; последнего owner сервер не даст)."""
    target = users.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    reason = _delete_denied(actor, target)
    if reason:
        raise HTTPException(status_code=400 if target.get("id") == actor.get("id") else 403,
                            detail="Нельзя удалить самого себя" if target.get("id") == actor.get("id")
                            else "Администратор удаляет только сотрудников своего подразделения")
    try:
        deleted = users.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    auth.drop_user_sessions(user_id)
    activitylog.log("action", user=actor, path=f"/users/{user_id}",
                    detail={"action": "user_delete", "target": target.get("username") or user_id})
    return {"user_id": user_id, "deleted": True}


@router.post("/users/{user_id}/role", dependencies=owner_only)
def change_user_role(user_id: str, req: RoleRequest, actor: dict = Depends(require_owner)):
    """Назначить администратором или убрать из администраторов — только главный."""
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя изменить собственную роль")
    try:
        user = users.set_role(user_id, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Права изменились — пусть перезайдёт с актуальной ролью
    auth.drop_user_sessions(user_id)
    activitylog.log("action", user=actor, path=f"/users/{user_id}/role",
                    detail={"action": "role_change", "target": user.get("username"), "role": req.role})
    return users.public_view(user)


@router.post("/users/{user_id}/active")
def change_user_active(user_id: str, req: ActiveRequest, actor: dict = Depends(require_admin)):
    """
    Подтверждение регистрации и блокировка доступа. Администратор может
    активировать и блокировать сотрудников, главный администратор — кого угодно.
    """
    _target_user(user_id, actor)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")
    try:
        user = users.set_active(user_id, req.active)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.active:
        auth.drop_user_sessions(user_id)
    return users.public_view(user)


@router.post("/users/{user_id}/credentials")
def set_user_credentials(user_id: str, req: TargetCredentialsRequest,
                         actor: dict = Depends(require_admin)):
    """
    Новый временный пароль (пусто — сгенерируем). Логин вручную не меняется: если его ещё
    нет (профиль-вакансия), он выдаётся один раз из ФИО. Сотрудник при входе задаст свой пароль.
    """
    target = _target_user(user_id, actor)
    password = req.password or staffing.temp_password()
    try:
        if not target.get("username"):
            users.set_username(user_id, staffing.new_username(target.get("full_name") or ""))
        # Сотруднику — выданный пароль (виден админу); админу — сменит при входе.
        users.set_password(user_id, password, must_change=target.get("role") != users.ROLE_EMPLOYEE,
                           issued=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    auth.drop_user_sessions(user_id)
    updated = users.get_user(user_id)
    auth.clear_failures(updated.get("username") or "")      # новый пароль снимает блокировку входа
    activitylog.log("action", user=actor, path=f"/users/{user_id}/credentials",
                    detail={"action": "password_issued", "target": updated.get("username")})
    return {**users.public_view(updated), "temp_password": password}


@router.post("/users/{user_id}/pause")
def pause_user(user_id: str, req: PauseRequest, actor: dict = Depends(require_admin)):
    """Приостановить адаптацию (больничный и т.п.) или возобновить: пока пауза, план стоит,
    после неё продолжается с того же места. То же сотрудник может сделать сам в кабинете
    («Я на больничном»)."""
    _target_user(user_id, actor)
    return users.public_view(messaging.set_sick(user_id, req.paused))


@router.post("/users/{user_id}/transfer-ownership", dependencies=owner_only)
def transfer_ownership(user_id: str, actor: dict = Depends(require_owner)):
    """Передача роли главного администратора. Прежний владелец остаётся админом."""
    try:
        new_owner = users.transfer_ownership(actor["id"], user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Роли поменялись у обоих — обе сессии переоформляются входом заново
    auth.drop_user_sessions(user_id)
    auth.drop_user_sessions(actor["id"])
    return users.public_view(new_owner)


@router.get("/users/{user_id}/schedule")
def get_user_schedule(user_id: str, actor: dict = Depends(require_admin)):
    """
    Персональное расписание: план-шаблон, пересчитанный на дату выхода этого
    сотрудника, с подстановкой плейсхолдеров. Считается на лету — при правке
    плана или даты выхода расписание всегда актуальное.
    """
    user = _target_user(user_id, actor)
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users/{user_id}/schedule/materialize")
def materialize_user_schedule(user_id: str, actor: dict = Depends(require_admin)):
    """Пересобрать расписание-инстансы сотрудника для доставки по времени (инбокс).
    Планировщик и сам досоздаёт недостающее, но после смены плана/даты выхода это
    сразу обновляет будущие (ещё не доставленные) сообщения."""
    user = _target_user(user_id, actor)
    count = messaging.materialize_employee(user, force=True)
    return {"materialized": count}


class TestMessage(BaseModel):
    title: str | None = None
    body: str | None = None
    delay: int | None = Field(default=0, ge=0, le=7 * 24 * 3600)   # секунд от «сейчас»; 0 — сразу


class NotifyTestRequest(BaseModel):
    messages: list[TestMessage] = Field(default_factory=list, max_length=50)


@router.post("/users/{user_id}/notify-test", dependencies=globaltest_only)
def notify_test(user_id: str, req: NotifyTestRequest, actor: dict = Depends(require_admin)):
    """Тестировщик уведомлений: кладёт несколько сообщений в инбокс выбранного
    пользователя. Он увидит их очередью на своей странице (кабинет/админка)."""
    target = _target_user(user_id, actor)
    msgs = [m for m in req.messages if (m.title or "").strip() or (m.body or "").strip()]
    if not msgs:
        raise HTTPException(status_code=400, detail="Нет сообщений для отправки")
    for m in msgs:
        messaging.push_test(target["id"], (m.title or "").strip(), (m.body or "").strip(),
                            delay_seconds=m.delay or 0)
    scheduled = sum(1 for m in msgs if (m.delay or 0) > 0)
    return {"sent": len(msgs), "scheduled": scheduled,
            "unread": messaging.unread_count(target["id"]),
            "target": target.get("full_name") or target.get("username") or target["id"]}


class TestTypedMessages(BaseModel):
    messages: list[dict] | None = Field(default=None, max_length=50)   # None — по примеру каждого типа


@router.get("/test-messages/samples", dependencies=globaltest_only)
def test_message_samples():
    """Примеры тестовых сообщений всех типов — заготовка для редактора на странице /message-test."""
    return {"messages": messaging.test_samples()}


@router.post("/users/{user_id}/test-messages", dependencies=globaltest_only)
def send_test_messages(user_id: str, req: TestTypedMessages, actor: dict = Depends(require_admin)):
    """Тестовые сообщения выбранному пользователю: тип, заголовок, текст, пункты чек-листа,
    вопросы опроса/теста и задержку задаёт администратор. Приходят в инбокс и пушем."""
    target = _target_user(user_id, actor)
    items = [m for m in (req.messages or []) if isinstance(m, dict)] if req.messages is not None else None
    if items is not None and not items:
        raise HTTPException(status_code=400, detail="Нет сообщений для отправки")
    ids = messaging.send_test_messages(target["id"], items)
    activitylog.log("action", user=actor, path=f"/users/{user_id}/test-messages",
                    detail={"action": "test_messages", "count": len(ids)})
    return {"sent": len(ids), "target": target.get("full_name") or target.get("username") or target["id"]}


EMPLOYEE_EXPORTS = {"schedule.json", "schedule.md"}


@router.get("/users/{user_id}/export/{name}")
def export_user_schedule(user_id: str, name: str, actor: dict = Depends(require_admin)):
    if name not in EMPLOYEE_EXPORTS:
        raise HTTPException(status_code=400, detail=f"Доступны: {', '.join(EMPLOYEE_EXPORTS)}")

    employee = _target_user(user_id, actor)
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if name == "schedule.json":
        body = json.dumps(schedule, ensure_ascii=False, indent=2)
        media_type = "application/json"
    else:
        body = planner.render_schedule_md(schedule)
        media_type = "text/markdown; charset=utf-8"

    # ФИО кириллицей в заголовок напрямую не положить (HTTP-заголовки — latin-1),
    # поэтому ASCII-запаска плюс RFC 5987 filename* с процентным кодированием
    pretty = f"{(employee.get('full_name') or 'employee').replace(' ', '_')}_{name}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{user_id}_{name}"; '
                 f"filename*=UTF-8''{quote(pretty)}"},
    )
