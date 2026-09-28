"""
Общие зависимости веб-слоя: кто вошёл, что ему можно видеть и менять.

Здесь живут проверки доступа (current_user -> require_setup_done -> require_admin /
require_owner) и правила разграничения между администраторами. Роутеры api_*.py
импортируют их отсюда, а не заводят свои копии, — иначе правило «администратор
видит только своё» пришлось бы поддерживать в нескольких местах.
"""

import hmac
import hashlib
import os
import threading
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import auth
import users
import indexing

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _bg(fn, *args):
    """Фоновая задача (реанализ базы и т.п. — может быть долгой из-за LLM)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


SPA_INDEX = STATIC_DIR / "app" / "index.html"
_NOT_BUILT = ("<!DOCTYPE html><meta charset='utf-8'><title>НейроМастер</title>"
              "<p>Интерфейс не собран: выполните <code>cd frontend && npm ci && npm run build</code>.</p>")


def spa_html() -> HTMLResponse:
    """Страница сайта: собранное SPA (frontend/ -> static/app/index.html)."""
    try:
        return HTMLResponse(SPA_INDEX.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(_NOT_BUILT, status_code=503)


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        auth.COOKIE_NAME, token,
        httponly=True,          # кука недоступна из JS — защита от кражи через XSS
        samesite="lax",         # браузер не пришлёт её при кросс-сайтовых POST — базовая защита от CSRF
        secure=auth.COOKIE_SECURE,  # только по HTTPS — токен не утечёт по чистому HTTP (MITM)
        max_age=auth.SESSION_TTL,
        path="/",
    )


# ---------- Доступ ----------
def _session_token(request: Request) -> str | None:
    """Токен сессии: заголовок Authorization: Bearer <token> (нативные приложения,
    хранят токен в SecureStore) ИЛИ cookie nm_session (браузер). Один опаковый токен
    из таблицы sessions — источник один, доставка две."""
    h = request.headers.get("authorization") or request.headers.get("Authorization")
    if h and h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.cookies.get(auth.COOKIE_NAME)


def current_user(request: Request) -> dict:
    """Любой вошедший пользователь. Без валидной сессии — 401."""
    user = auth.get_session_user(_session_token(request))
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_setup_done(user: dict = Depends(current_user)) -> dict:
    """
    Пока главный администратор не задал свои логин и пароль, дальше первичной
    настройки его не пускаем — иначе сгенерированные данные так и останутся жить.
    """
    if user.get("must_change_credentials"):
        raise HTTPException(status_code=403, detail="Сначала задайте свои логин и пароль")
    return user


def require_admin(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_admin(user):
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
    return user


def require_owner(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_owner(user):
        raise HTTPException(status_code=403,
                            detail="Это может сделать только главный администратор")
    return user


logged_in = [Depends(require_setup_done)]
admin_only = [Depends(require_admin)]
owner_only = [Depends(require_owner)]


# ---------- Разграничение видимости между администраторами ----------
# Суперадмин (owner) видит всё, что загрузили администраторы, и всех людей.
# Администратор — только СВОИ загруженные документы и людей СВОЕГО отдела.
# Сотрудник (employee) сюда не попадает: админские ручки закрыты require_admin.
can_see_doc = users.can_see_doc          # правила — в users.py (там же и тесты)


def visible_documents(user: dict) -> list:
    return users.visible_docs(user, indexing.list_documents())


def ensure_doc_access(user: dict, filename: str, write: bool = False):
    """403, если администратор обращается к чужому документу (write=True — к общему
    документу суперадмина тоже: его можно смотреть, но не менять). Незнакомое имя
    пропускаем — свой 404 отдаст сам обработчик."""
    if users.is_owner(user):
        return
    doc = next((d for d in indexing.list_documents() if d.get("filename") == filename), None)
    if doc is None:
        return
    if not can_see_doc(user, doc):
        raise HTTPException(status_code=403, detail="Документ загружен другим администратором")
    if write and not users.can_edit_doc(user, doc):
        raise HTTPException(status_code=403,
                            detail="Общий документ суперадмина: менять и удалять его может только суперадмин")


# ---------- /globaltest: тесты и диагностика под отдельным паролем ----------
# Пароль хранится только хешем (scrypt, как у пользователей) в env:
#   NEIROMASTER_GLOBALTEST_PASSWORD_HASH=<salt_hex>:<hash_hex>
# Получить: python scripts/globaltest_hash.py. Не задан — раздел закрыт для всех.
# После ввода пароля — подписанная кука на GT_TTL (ключ HMAC — сам хеш: сменили пароль —
# все выданные доступы недействительны). Нужен ещё и вход администратора.
GT_COOKIE = "nm_gt"
GT_TTL = 12 * 3600


def _gt_hash() -> str:
    return os.environ.get("NEIROMASTER_GLOBALTEST_PASSWORD_HASH", "").strip()


def gt_configured() -> bool:
    return ":" in _gt_hash()


def gt_check_password(password: str) -> bool:
    if not gt_configured():
        return False
    salt, digest = _gt_hash().split(":", 1)
    return users.verify_password(password or "", salt, digest)


def _gt_sign(user_id: str, exp: int) -> str:
    return hmac.new(_gt_hash().encode(), f"{user_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def gt_token(user_id: str, now: float | None = None) -> str:
    exp = int(now or time.time()) + GT_TTL
    return f"{exp}.{_gt_sign(user_id, exp)}"


def gt_unlocked(request: Request, user: dict | None) -> bool:
    raw = request.cookies.get(GT_COOKIE) or ""
    exp, _, sig = raw.partition(".")
    if not (user and gt_configured() and exp.isdigit() and int(exp) > time.time()):
        return False
    return hmac.compare_digest(sig, _gt_sign(user["id"], int(exp)))


def require_globaltest(request: Request, user: dict = Depends(require_admin)) -> dict:
    """API тестовых страниц: администратор + открытый паролем раздел /globaltest."""
    if not gt_unlocked(request, user):
        raise HTTPException(status_code=403, detail="Раздел тестирования закрыт — откройте /globaltest")
    return user


globaltest_only = [Depends(require_globaltest)]


def page_for_globaltest(request: Request):
    """Тестовая страница: как админская, но без открытого паролем раздела — на /globaltest."""
    page = page_for_admin(request)
    if isinstance(page, RedirectResponse):
        return page
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if not gt_unlocked(request, user):
        return RedirectResponse(url="/globaltest", status_code=303)
    return page


def page_for_admin(request: Request):
    """
    Страница админки: вошёл -> прошёл первичную настройку -> администратор.
    Один хелпер вместо одинаковых блоков в маршрутах страниц.
    """
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    if not users.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return spa_html()
