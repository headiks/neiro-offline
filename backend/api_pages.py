"""
HTML-страницы приложения. Логики нет — только проверка доступа и отдача файла.

Сайт — одностраничное приложение (frontend/, собирается в static/app/): каждая страница
отдаёт один и тот же static/app/index.html, дальше маршрут разбирает React Router.
Проверки доступа остаются на сервере: без входа — /login, с временным паролем — /setup,
админские страницы — только администраторам (deps.page_for_admin).
"""

import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import activitylog
import auth
import security
import users
from deps import (BASE_DIR, STATIC_DIR, GT_COOKIE, GT_TTL, gt_check_password, gt_configured, gt_token,
                  gt_unlocked, page_for_admin, page_for_globaltest, require_admin, spa_html)

router = APIRouter()

# Разделы админки (/admin/<раздел>) и служебные страницы — все внутри SPA.
ADMIN_SECTIONS = ("users", "plans", "documents", "messages", "questions")
# Журнал действий — рабочий инструмент администратора. Тесты и диагностика — только через
# /globaltest с отдельным паролем (deps.page_for_globaltest).
SERVICE_PAGES = ("/logs", "/globaltest")
TEST_PAGES = ("/s3", "/documents-board", "/documents-table", "/plans-db", "/notify-test",
              "/doc-breakdown", "/queue-test", "/message-test")


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    """Личный кабинет сотрудника: сообщения плана, прогресс, ассистент."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    return spa_html()


@router.get("/login", response_class=HTMLResponse)
def login_page():
    return spa_html()


@router.get("/register", response_class=HTMLResponse)
def register_page():
    return spa_html()


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    """Первичная настройка: замена выданного пароля своим."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("must_change_credentials"):
        return RedirectResponse(url="/admin" if users.is_admin(user) else "/", status_code=303)
    return spa_html()


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    """Админка: пользователи, планы, документы, сообщения, вопросы."""
    return page_for_admin(request)


@router.get("/admin/{section}", response_class=HTMLResponse)
def admin_section(section: str, request: Request):
    if section not in ADMIN_SECTIONS:
        return RedirectResponse(url="/admin", status_code=303)
    return page_for_admin(request)


def _service_page(path: str):
    def page(request: Request):
        return page_for_admin(request)
    page.__name__ = "page_" + path.strip("/").replace("-", "_")
    router.add_api_route(path, page, methods=["GET"], response_class=HTMLResponse)


def _test_page(path: str):
    def page(request: Request):
        return page_for_globaltest(request)
    page.__name__ = "page_" + path.strip("/").replace("-", "_")
    router.add_api_route(path, page, methods=["GET"], response_class=HTMLResponse)


for _path in SERVICE_PAGES:
    _service_page(_path)
for _path in TEST_PAGES:
    _test_page(_path)


class GlobaltestUnlock(BaseModel):
    password: str = Field(max_length=200)


@router.get("/api/globaltest")
def globaltest_status(request: Request, user: dict = Depends(require_admin)):
    return {"configured": gt_configured(), "unlocked": gt_unlocked(request, user)}


@router.post("/api/globaltest/unlock")
def globaltest_unlock(req: GlobaltestUnlock, request: Request, user: dict = Depends(require_admin)):
    """Открыть раздел тестирования паролем. Перебор ограничен: 5 попыток в 5 минут."""
    security.limit(request, "globaltest", 5, 300, key=user["id"])
    ok = gt_check_password(req.password)
    activitylog.log("action", user=user, request=request,
                    detail={"action": "globaltest_unlock", "ok": ok})
    if not ok:
        raise HTTPException(status_code=403, detail="Неверный пароль" if gt_configured()
                            else "Пароль раздела не настроен на сервере")
    resp = JSONResponse({"unlocked": True})
    resp.set_cookie(GT_COOKIE, gt_token(user["id"]), max_age=GT_TTL, httponly=True,
                    samesite="strict", secure=auth.COOKIE_SECURE, path="/")
    return resp


@router.post("/api/globaltest/lock")
def globaltest_lock(user: dict = Depends(require_admin)):
    resp = JSONResponse({"unlocked": False})
    resp.delete_cookie(GT_COOKIE, path="/")
    return resp


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Браузеры просят /favicon.ico сами — отдаём логотип, а не 404 в консоли."""
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------- Приложение для Android ----------
# APK кладётся на сервер при выкатке (в git не хранится). Страница и файл открыты без входа:
# сотрудник скачивает приложение прямо на телефон, а войти можно уже в самом приложении.
def _apk_path() -> Path:
    return Path(os.environ.get("NEIROMASTER_APK") or BASE_DIR / "data" / "app" / "NeiroMaster.apk")


@router.get("/app", response_class=HTMLResponse)
def app_page():
    return spa_html()


@router.get("/api/app/android")
def android_app_info():
    """Есть ли APK, размер и дата сборки — для страницы «Приложение»."""
    try:
        st = _apk_path().stat()
    except FileNotFoundError:
        return {"available": False}
    return {"available": True, "url": "/app/android.apk", "size": st.st_size,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))}


@router.get("/app/android.apk", include_in_schema=False)
def android_apk():
    path = _apk_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Приложение пока не загружено на сервер")
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename="NeiroMaster.apk")
