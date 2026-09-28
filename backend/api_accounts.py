"""Вход, регистрация, свой профиль и свой личный кабинет."""

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import auth
import users
import questions
import security
import employees as adaptation
import messaging
import activitylog
from deps import _set_session_cookie, _session_token, current_user, require_setup_done, logged_in

router = APIRouter()

# Самостоятельная регистрация (аккаунт ждёт подтверждения администратора). В закрытом
# контуре, где все доступы выдаёт администратор, её выключают: NEIROMASTER_ALLOW_REGISTRATION=0.
ALLOW_REGISTRATION = os.environ.get("NEIROMASTER_ALLOW_REGISTRATION", "1").lower() not in ("0", "false", "no")

LOGIN_RATE_PER_MIN = int(os.environ.get("NEIROMASTER_LOGIN_RATE", "120"))

# Пароль длиннее — не пароль, а попытка нагрузить scrypt.
_PASSWORD = Field(max_length=256)
_ANSWERS_MAX_BYTES = 32 * 1024


class LoginRequest(BaseModel):
    username: str = Field(max_length=128)
    password: str = _PASSWORD


class RegisterRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = _PASSWORD
    full_name: str = Field(max_length=300)
    position: str | None = Field(default=None, max_length=300)
    contact: str | None = Field(default=None, max_length=300)


class CredentialsRequest(BaseModel):
    username: str | None = None     # логин не меняется: выдан один раз из ФИО
    password: str = _PASSWORD


class PasswordChangeRequest(BaseModel):
    old_password: str = _PASSWORD
    new_password: str = _PASSWORD


class TestNotification(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=4000)


class SickRequest(BaseModel):
    sick: bool


# ---------- Вход, регистрация, свой профиль ----------
@router.post("/api/login")
def api_login(req: LoginRequest, request: Request, response: Response):
    client = security.client_ip(request)
    # Попыток входа с одного адреса в минуту (перебор по многим логинам сразу); перебор
    # одного логина отдельно ограничивает лок-аут в auth.login. Щедро: в компании за NAT
    # вся смена ходит с одного адреса.
    security.limit(request, "login", LOGIN_RATE_PER_MIN, 60)
    try:
        token, user = auth.login(req.username, req.password, client=client)
    except ValueError as e:
        activitylog.log("login_failed", request=request,
                        detail={"username": (req.username or "").strip().lower()})
        raise HTTPException(status_code=401, detail=str(e))
    activitylog.log("login", user=user, request=request)
    _set_session_cookie(response, token)
    return {
        "username": user["username"],
        "role": user["role"],
        "must_change_credentials": bool(user.get("must_change_credentials")),
        # Токен в теле — для нативных приложений (Bearer). Браузер использует cookie выше.
        "token": token,
    }


@router.post("/api/register")
def api_register(req: RegisterRequest, request: Request):
    """Самостоятельная регистрация сотрудника (ждёт подтверждения администратора)."""
    if not ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="Регистрация отключена — доступ выдаёт администратор")
    security.limit(request, "register", 5, 3600)
    try:
        user = users.register_employee(req.username, req.password, req.full_name,
                                       position=req.position or "", contact=req.contact or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": user["id"],
        "username": user["username"],
        "active": user["active"],
        "needs_approval": not user["active"],
    }


@router.get("/api/config")
def api_config():
    """Публичные настройки для страниц входа (без секретов)."""
    return {"registration": ALLOW_REGISTRATION}


@router.post("/api/logout")
def api_logout(request: Request, response: Response):
    # Браузер — кука, приложение — Bearer: разлогиниваем тот токен, которым пришли.
    token = _session_token(request)
    activitylog.log("logout", user=auth.get_session_user(token), request=request)
    auth.logout(token)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"logged_out": True}


@router.get("/api/me")
def api_me(user: dict = Depends(current_user)):
    return users.public_view(user)


@router.post("/api/setup-credentials")
def api_setup_credentials(req: CredentialsRequest, response: Response,
                          user: dict = Depends(current_user)):
    """
    Первичная настройка: пользователь заменяет выданный пароль своим (логин остаётся).
    Доступна только тем, у кого стоит флаг must_change_credentials.
    """
    if not user.get("must_change_credentials"):
        raise HTTPException(status_code=400, detail="Учётные данные уже настроены")
    try:
        users.set_credentials(user["id"], None, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Логин сменился — старые сессии больше не действуют
    auth.drop_user_sessions(user["id"])
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@router.post("/api/password")
def api_change_password(req: PasswordChangeRequest, response: Response,
                        user: dict = Depends(current_user)):
    if user.get("role") == users.ROLE_EMPLOYEE:
        raise HTTPException(status_code=403, detail="Пароль сотрудника меняет администратор")
    try:
        auth.change_own_password(user, req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Смена пароля разлогинивает все сессии, включая текущую
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@router.get("/api/my/schedule", dependencies=logged_in)
def api_my_schedule(user: dict = Depends(require_setup_done)):
    """Свой план адаптации — то, что сотрудник видит в личном кабинете."""
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/my/questions", dependencies=logged_in)
def api_my_questions(user: dict = Depends(require_setup_done)):
    """Свои эскалированные вопросы и ответы на них от администратора."""
    return {"questions": questions.list_for_user(user["id"])}


@router.get("/api/my/messages", dependencies=logged_in)
def api_my_messages(user: dict = Depends(require_setup_done)):
    """Инбокс: сообщения плана, которые уже наступили по расписанию и доставлены."""
    return {"messages": messaging.inbox(user["id"]),
            "unread": messaging.unread_count(user["id"])}


@router.post("/api/my/messages/{message_id}/read", dependencies=logged_in)
def api_mark_message_read(message_id: str, user: dict = Depends(require_setup_done)):
    """Отметить доставленное сообщение прочитанным."""
    if not messaging.mark_read(user["id"], message_id):
        raise HTTPException(status_code=404, detail="Сообщение не найдено или уже прочитано")
    return {"read": True}


class AnswersRequest(BaseModel):
    answers: dict = Field(default_factory=dict)


@router.post("/api/my/messages/{message_id}/answer", dependencies=logged_in)
def api_answer_message(message_id: str, req: AnswersRequest,
                       user: dict = Depends(require_setup_done)):
    """Ответы на чек-лист/опрос/тест из инбокса (приложение и кабинет — одно хранилище)."""
    if len(json.dumps(req.answers, ensure_ascii=False).encode("utf-8")) > _ANSWERS_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Слишком большой ответ")
    if not messaging.save_answers(user["id"], message_id, req.answers):
        raise HTTPException(status_code=404, detail="Сообщение не найдено")
    return {"saved": True}


@router.post("/api/my/status", dependencies=logged_in)
def api_set_my_status(req: SickRequest, user: dict = Depends(require_setup_done)):
    """Сотрудник сам ставит/снимает больничный: план встаёт на паузу, после выхода
    продолжается с того же места (messaging.set_sick). Возвращает актуальный статус."""
    was_sick = user.get("status") == "paused"
    updated = messaging.set_sick(user["id"], req.sick)
    if was_sick != req.sick:
        activitylog.log("action", user=user, path="/api/my/status",
                        detail={"action": "sick_on" if req.sick else "sick_off"})
    return {"status": updated["status"], "sick": updated["status"] == "paused"}


@router.post("/api/my/messages/test", dependencies=logged_in)
def api_test_notification(req: TestNotification, user: dict = Depends(require_setup_done)):
    """Ручная отправка тестового уведомления себе — для проверки очереди и всплывашек."""
    row_id = messaging.push_test(user["id"], (req.title or "").strip(), (req.body or "").strip())
    return {"ok": True, "id": row_id}


class PushTokenRequest(BaseModel):
    token: str = Field(max_length=4096)
    platform: str | None = Field(default=None, max_length=32)


@router.post("/api/my/push-token", dependencies=logged_in)
def api_register_push_token(req: PushTokenRequest, user: dict = Depends(require_setup_done)):
    """Регистрация push-токена устройства (Expo) сотрудника — приложение шлёт после логина
    и при смене токена. По нему приходят пуши о доставке сообщений плана."""
    import push
    if not push.register_token(user["id"], (req.token or "").strip(), (req.platform or "").strip()):
        raise HTTPException(status_code=400, detail="Пустой токен")
    return {"ok": True}


@router.delete("/api/my/push-token", dependencies=logged_in)
def api_remove_push_token(req: PushTokenRequest, user: dict = Depends(require_setup_done)):
    """Отвязать push-токен (выход из аккаунта / отключение уведомлений на устройстве)."""
    import push
    push.remove_token((req.token or "").strip(), user_id=user["id"])
    return {"ok": True}
