"""
Защитный слой веб-приложения: заголовки безопасности, проверка Origin и ограничение
частоты запросов.

Что закрывает:
  * заголовки ответа — запрет встраивания в чужие фреймы (clickjacking), запрет
    угадывания MIME, политика источников скриптов (CSP), HSTS при HTTPS;
  * CSRF «второй линией» — браузерный запрос с сессионной кукой, изменяющий данные
    (POST/PUT/PATCH/DELETE), принимается только со своего Origin. Первая линия —
    SameSite=Lax у куки. Нативное приложение ходит с Bearer-токеном и без куки —
    его проверка не касается;
  * перебор и спам — счётчик запросов в окне (вход, регистрация, вопросы к ИИ).
    При Redis счётчик общий для всех воркеров, без него — в памяти процесса.
"""

import os
import time
import threading
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from redis_conn import get_redis

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Скрипты — только свои файлы: сайт (static/app, сборка frontend/) не использует inline-скрипты
# и обработчики, поэтому внедрённый в страницу <script> не выполнится. Внешние CDN запрещены —
# шрифт и иконки собраны в бандл. Стили: 'unsafe-inline' нужен для style-атрибутов React.
# connect-src 'self' не даёт XSS утащить данные на чужой сервер через fetch.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))


def security_headers(secure: bool) -> dict:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": CSP,
    }
    if secure:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def _allowed_origins() -> set:
    extra = os.environ.get("NEIROMASTER_TRUSTED_ORIGINS", "")
    return {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}


def origin_ok(request: Request, cookie_name: str) -> bool:
    """False — браузерный изменяющий запрос по куке пришёл с чужого сайта."""
    if request.method not in UNSAFE_METHODS:
        return True
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") or cookie_name not in request.cookies:
        return True                       # приложение (Bearer) или анонимный запрос
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True                       # старые браузеры/curl: остаётся SameSite=Lax
    parts = urlsplit(origin)
    source = f"{parts.scheme}://{parts.netloc}".rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if parts.netloc and parts.netloc == host:
        return True
    return source in _allowed_origins()


def install(app, cookie_name: str, secure: bool):
    """Подключает заголовки и проверку Origin ко всем ответам приложения."""
    headers = security_headers(secure)

    @app.middleware("http")
    async def _security(request: Request, call_next):
        if not origin_ok(request, cookie_name):
            response = JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)
        else:
            response = await call_next(request)
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        # Ответы API с персональными данными не должны оседать в кэше браузера/прокси.
        path = request.url.path
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


# ---------- Ограничение частоты ----------
_mem: dict = {}
_mem_lock = threading.Lock()


def hit(key: str, limit: int, window: int) -> bool:
    """Засчитать запрос. False — лимит в окне window секунд исчерпан."""
    r = get_redis()
    if r is not None:
        try:
            k = f"nm:rl:{key}"
            pipe = r.pipeline()
            pipe.incr(k)
            pipe.expire(k, window, nx=True)
            count = pipe.execute()[0]
            return int(count) <= limit
        except Exception:
            pass                           # Redis отвалился — считаем в памяти
    now = time.time()
    with _mem_lock:
        start, count = _mem.get(key, (now, 0))
        if now - start >= window:
            start, count = now, 0
        count += 1
        _mem[key] = (start, count)
        if len(_mem) > 50000:              # не даём словарю расти бесконечно
            for old in [k for k, (s, _) in _mem.items() if now - s >= window][:10000]:
                _mem.pop(old, None)
        return count <= limit


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(request: Request, name: str, limit_: int, window: int, key: str = ""):
    """429, если с этого адреса (или по ключу) слишком много запросов."""
    if not hit(f"{name}:{key or client_ip(request)}", limit_, window):
        raise HTTPException(status_code=429,
                            detail="Слишком много запросов. Подождите немного и повторите.")


def reset(key_prefix: str = ""):
    """Сброс счётчиков в памяти (для тестов)."""
    with _mem_lock:
        for k in [k for k in _mem if k.startswith(key_prefix)]:
            _mem.pop(k, None)
