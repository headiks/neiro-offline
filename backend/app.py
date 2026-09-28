"""
Точка входа приложения: сборка FastAPI из роутеров и разовые действия при старте.

Маршруты живут в api_*.py (страницы, аккаунты, чат, документы, знания, планы, люди),
общие проверки доступа — в deps.py, защитный слой (заголовки, Origin, лимиты) — в
security.py. Здесь остаётся только то, что касается приложения целиком: схема БД,
миграции со старых форматов, подключение роутеров.

ВАЖНО: порядок include_router повторяет прежний порядок объявления маршрутов —
FastAPI выбирает первый подходящий шаблон, и переставлять роутеры нельзя.
"""

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import uvicorn

import db
import auth
import users
import folders
import stages
import planner
import indexing
import documents
import docregistry
import questions
import messaging
import activitylog
import security
import pii_key
import rawdb
from deps import BASE_DIR, STATIC_DIR

import api_pages
import api_accounts
import api_chat
import api_documents
import api_knowledge
import api_plans
import api_people
import api_activity
import api_queue

ROUTERS = (api_pages, api_accounts, api_chat, api_documents,
           api_knowledge, api_plans, api_people, api_activity, api_queue)


def _step(name: str, fn):
    """Шаг старта, который не должен ронять приложение: ошибка — в лог, работа дальше."""
    try:
        return fn()
    except Exception as e:
        print(f"Предупреждение: {name}: {e}")
        return None


def _seed_knowledge():
    """Стартовая структура знаний (этапы + смысловые папки) — только если таблицы пусты."""
    seed_path = BASE_DIR / "data" / "knowledge_seed.json"
    if not seed_path.exists():
        print(f"ВНИМАНИЕ: нет файла сида {seed_path} — стартовые папки не заведены.")
        return
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    s = stages.seed_if_empty(seed.get("stages", []))
    f = folders.seed_if_empty(seed.get("folders", []))
    if s or f:
        print(f"Стартовая структура знаний: засеяно этапов {s}, папок {f}.")


def _init_docpipe():
    import docpipe
    docpipe.init_schema()
    docpipe.sync_plan_from_catalog()


def _migrate_legacy():
    """Разовые переносы со старых файловых хранилищ в БД (идемпотентно)."""
    moved = {
        "планов": planner.migrate_plans_from_files(),
        "аккаунтов из users.json": users.migrate_legacy_json_users(),
        "сотрудников из employees.json": users.migrate_legacy_employees(),
        "документов из registry.json": docregistry.migrate_from_file(),
        "вопросов из pending_questions.json": questions.migrate_from_file(),
        "дат выхода к формату ГГГГ-ММ-ДД": users.migrate_start_dates(),
        "шифрование ПДн пользователей": users.encrypt_plaintext(),
        "шифрование ПДн в вопросах": questions.encrypt_plaintext(),
    }
    for what, n in moved.items():
        if n:
            print(f"Миграция: перенесено/исправлено {what}: {n}")


def _announce_owner():
    initial = users.ensure_owner()
    if initial:
        print("=" * 70)
        print("Создана учётная запись главного администратора.")
        print(f"  Логин:  {initial['username']}")
        print(f"  Пароль: {initial['password']}")
        print(f"  Дубль записан в {users.INITIAL_CREDENTIALS_PATH}")
        print("  При первом входе система попросит задать свой пароль.")
        print("=" * 70)


def _prune_orphans():
    """Самоочистка «призрачных» источников: docpipe-документы, которых уже нет в реестре,
    продолжали бы цитироваться в генерации и ответах. Защита от вайпа при пустом реестре —
    внутри prune_orphans."""
    import docpipe
    keep = {e.get("filename") for e in docregistry.list_documents()}
    keep |= {d.get("filename") for d in documents.list_meta()}
    removed = docpipe.prune_orphans(keep)
    if removed:
        print(f"docpipe: удалено осиротевших документов (нет в реестре): {removed}")


def _requeue_without_redis():
    """Возврат зависших задач в очередь. При Redis это делает ОДИН worker-процесс
    (worker.py, под общим замком) — иначе каждый web-воркер поставил бы дубли."""
    from redis_conn import redis_available
    if redis_available():
        return
    import docpipe
    _step("возврат зависших документов в очередь", indexing.requeue_stranded)
    _step("возобновление разметки docpipe", docpipe.requeue_stranded)


def _autoassign_plan():
    """Активный общий план — сотрудникам без плана (ручные назначения не трогаются)."""
    import autoplan
    assigned = autoplan.assign_unassigned()
    if assigned:
        print(f"autoplan: назначен активный общий план {assigned} сотрудникам без плана")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.startup_lock():                  # схема и миграции — по одному воркеру за раз
        db.init_schema()                     # до первого обращения к аккаунтам
        rawdb.init_schema()                  # отдельная БД исходников (если задана)
        # Ключ шифрования ПДн: из окружения, из файла или новый. Ключ не совпал с тем,
        # которым зашифрованы данные, — старт прерывается (иначе ПДн стали бы нечитаемы).
        pii_key.ensure()
        docregistry.init()
        questions.init()
        _step("реестр документов", documents.init)
        _step("посев структуры знаний", _seed_knowledge)
        _step("пайплайн разметки docpipe", _init_docpipe)
        _step("миграция старых данных", _migrate_legacy)
        _announce_owner()
        _step("очистка осиротевших меток docpipe", _prune_orphans)
        _requeue_without_redis()
        _step("автоназначение плана", _autoassign_plan)
    # Фоновый планировщик доставки сообщений плана. NEIROMASTER_SCHEDULER=0 — выключить
    # (когда доставку гоняют внешним cron: python dispatch_messages.py).
    messaging.start_scheduler()
    yield


# docs_url/redoc_url/openapi_url=None: служебные страницы FastAPI (Swagger и схема)
# открыты анониму и раскрывают полный список ручек — на проде не нужны.
app = FastAPI(title="НейроМастер", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# CORS — для веб-сборки мобильного приложения (react-native-web), которая ходит к API
# с другого origin. Нативные iOS/Android не подчиняются CORS. Приложение авторизуется
# по Bearer-токену (не cookie), поэтому allow_credentials не нужен (кука браузера на
# чужой origin не уходит); список origin — из NEIROMASTER_CORS_ORIGINS (через запятую),
# плюс локальные порты Expo для разработки.
_cors = [o.strip() for o in os.environ.get("NEIROMASTER_CORS_ORIGINS", "").split(",") if o.strip()]
_cors += ["http://localhost:8081", "http://localhost:19006", "http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


class _Static(StaticFiles):
    """Сборка сайта: файлы в static/app/assets/ с хешем в имени — кэшируются навсегда
    (новая сборка = новые имена), остальное браузер перепроверяет по ETag."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = f"{os.sep}app{os.sep}assets{os.sep}" in str(full_path)
        response.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if immutable
                                             else "no-cache")
        return response


app.mount("/static", _Static(directory=str(STATIC_DIR)), name="static")
# Сжатие ответов (скрипты сайта, JSON списков): через VPN/медленную сеть страницы
# открываются в разы быстрее. HTTPS-прокси может сжимать и сам — повторно не сожмётся.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def log_page_views(request, call_next):
    """Просмотры страниц — централизованно: GET-запрос, отдавший HTML (не /static, не /api).
    Так не нужно дублировать логирование в каждом обработчике страницы."""
    response = await call_next(request)
    path = request.url.path
    if (request.method == "GET" and response.status_code == 200
            and not path.startswith("/static") and not path.startswith("/api")
            and "text/html" in response.headers.get("content-type", "")):
        # Запись в БД — в пуле потоков: синхронный запрос на event loop стопорит весь воркер.
        await run_in_threadpool(_log_page_view, request, path)
    return response


def _log_page_view(request, path: str):
    try:
        user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
        activitylog.log("page_view", user=user, request=request, path=path)
    except Exception:
        pass


# Заголовки безопасности и проверка Origin — самым внешним слоем (добавлен последним).
security.install(app, cookie_name=auth.COOKIE_NAME, secure=auth.COOKIE_SECURE)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Живость для балансировщика и мониторинга: БД отвечает, Redis — если настроен."""
    from redis_conn import get_redis
    db.query("SELECT 1 AS ok", (), "one")
    r = get_redis()
    return {"ok": True, "db": True, "redis": bool(r is not None and r.ping()), "build": _build_name()}


def _build_name() -> str:
    """Какая сборка сайта сейчас отдаётся (index-<хеш>.js). Разные ответы при повторных
    запросах = за прокси работают разные копии приложения или кэш отдаёт старую страницу."""
    import re
    try:
        m = re.search(r"assets/(index-[\w-]+\.js)", (STATIC_DIR / "app" / "index.html").read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except OSError:
        return ""


for _module in ROUTERS:
    app.include_router(_module.router)


if __name__ == "__main__":
    # Разработка: python app.py. На проде — gunicorn (см. install.sh), за HTTPS-прокси.
    uvicorn.run(app, host=os.environ.get("NEIROMASTER_HOST", "127.0.0.1"),
                port=int(os.environ.get("NEIROMASTER_PORT", "8000")))
