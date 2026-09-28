"""
Push-уведомления на устройства сотрудников через FCM HTTP v1.

Приложение сотрудника (Expo, mobile/) получает нативный FCM-токен устройства
(getDevicePushTokenAsync) и регистрирует у нас (POST /api/my/push-token). Отсюда шлём
напрямую в FCM: POST https://fcm.googleapis.com/v1/projects/<project>/messages:send,
авторизация — OAuth2-токен, выписанный по service-account (Firebase). Файл ключа —
в env FCM_SERVICE_ACCOUNT или в secrets/fcm-service-account.json (в git не хранится).

Отправка — best-effort: сбой сети/FCM НЕ должен ронять доставку в инбокс (инбокс —
источник правды, пуш лишь дублирует). Протухшие токены (UNREGISTERED) чистим.

ponytail: шлём по одному сообщению на токен (FCM v1 без batch-эндпоинта) прямым
вызовом из планировщика; объёмы малы. Понадобится масштаб — вынести в RQ-задачу.
"""

import os
import json
from pathlib import Path

import requests

import db

_TIMEOUT = 15
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
BASE_DIR = Path(__file__).resolve().parents[1]   # корень проекта (код — в backend/)
# Docker: том секретов data/secrets/; установка без Docker — secrets/ в корне.
_SA_DEFAULT = next((p for p in (BASE_DIR / "data" / "secrets" / "fcm-service-account.json",
                                BASE_DIR / "secrets" / "fcm-service-account.json") if p.exists()),
                   BASE_DIR / "data" / "secrets" / "fcm-service-account.json")

_sa_cache = None   # (google credentials, project_id) — ленивое, кэшируется


# ---------- Хранилище токенов ----------
def register_token(user_id: str, token: str, platform: str = "") -> bool:
    """Привязать/обновить push-токен устройства к сотруднику. token уникален (PK):
    если он уже был у другого пользователя (сменили аккаунт на устройстве) —
    переназначаем на текущего."""
    token = (token or "").strip()
    if not token:
        return False
    db.execute(
        "INSERT INTO push_tokens (token, user_id, platform) VALUES (%s, %s, %s) "
        "ON CONFLICT (token) DO UPDATE SET user_id = EXCLUDED.user_id, "
        "platform = EXCLUDED.platform, updated_at = now()",
        (token, user_id, (platform or "")[:32]),
    )
    return True


def remove_token(token: str, user_id: str | None = None) -> None:
    """Отвязать токен. user_id — только свой: чужое устройство так не отключить."""
    token = (token or "").strip()
    if not token:
        return
    if user_id:
        db.execute("DELETE FROM push_tokens WHERE token = %s AND user_id = %s", (token, user_id))
    else:
        db.execute("DELETE FROM push_tokens WHERE token = %s", (token,))


def tokens_for_users(user_ids) -> dict:
    """{user_id: [token, ...]} для набора сотрудников. Пустой набор -> {}."""
    ids = [u for u in set(user_ids or []) if u]
    if not ids:
        return {}
    rows = db.query("SELECT user_id, token FROM push_tokens WHERE user_id = ANY(%s)", (ids,)) or []
    out: dict = {}
    for r in rows:
        out.setdefault(r["user_id"], []).append(r["token"])
    return out


# ---------- Отправка (FCM v1) ----------
def _access_token():
    """OAuth2-токен доступа и project_id из service-account. Кэшируем creds; google-auth
    сам обновляет протухший токен. Бросает при отсутствии/битом ключе (ловит вызвавший)."""
    global _sa_cache
    if _sa_cache is None:
        path = os.environ.get("FCM_SERVICE_ACCOUNT") or str(_SA_DEFAULT)
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(path, scopes=[_SCOPE])
        with open(path, "r", encoding="utf-8") as f:
            pid = json.load(f).get("project_id")
        _sa_cache = (creds, pid)
    creds, pid = _sa_cache
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return creds.token, pid


def _send_one(token: str, title: str, body: str, data: dict) -> str:
    """Одно сообщение в FCM v1. -> 'ok' | 'unregistered' | 'error'. Не бросает."""
    try:
        access, pid = _access_token()
    except Exception as e:
        print(f"[push] нет доступа к FCM (service-account): {e}")
        return "error"
    url = f"https://fcm.googleapis.com/v1/projects/{pid}/messages:send"
    message = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            # FCM data-поля — только строки.
            "data": {str(k): str(v) for k, v in (data or {}).items()},
            "android": {"priority": "high",
                        "notification": {"channel_id": "default", "sound": "default"}},
        }
    }
    try:
        r = requests.post(url, json=message, timeout=_TIMEOUT,
                          headers={"Authorization": f"Bearer {access}",
                                   "Content-Type": "application/json"})
        if r.status_code == 200:
            return "ok"
        err = ((r.json() if r.content else {}) or {}).get("error") or {}
        codes = {d.get("errorCode") for d in (err.get("details") or []) if isinstance(d, dict)}
        if err.get("status") in ("NOT_FOUND", "UNREGISTERED") or "UNREGISTERED" in codes:
            return "unregistered"   # токен мёртв (приложение удалено/переустановлено)
        print(f"[push] FCM {r.status_code}: {err.get('message') or r.text[:200]}")
        return "error"
    except Exception as e:
        print(f"[push] отправка не удалась: {e}")
        return "error"


def notify(items: list) -> int:
    """items — [{user_id, title, body, data?}]. Разворачивает в токены устройств и шлёт
    по одному в FCM. Протухшие токены (UNREGISTERED) удаляет. Возвращает число успешно
    отправленных. Ошибки не поднимает — доставка в инбокс важнее пуша."""
    if not items:
        return 0
    by_user = tokens_for_users([it.get("user_id") for it in items])
    if not by_user:
        return 0
    ok = 0
    for it in items:
        title = (it.get("title") or "НейроМастер")[:120]
        body = (it.get("body") or "")[:1000]
        data = it.get("data") or {}
        for tok in by_user.get(it.get("user_id"), []):
            res = _send_one(tok, title, body, data)
            if res == "ok":
                ok += 1
            elif res == "unregistered":
                remove_token(tok)
    return ok
