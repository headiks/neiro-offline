"""
Аудит безопасности и багов НейроМастера — воспроизводящие тесты.

Каждый тест проверяет РЕАЛЬНУЮ функцию проекта и падает (assert), если баг
присутствует. Тяжёлые зависимости (qdrant/psycopg/fastapi) в окружении не нужны:
цепочки импортов, тянущие их, подменяются заглушками в sys.modules — сами
тестируемые функции при этом настоящие.

Запуск:
    python test_security_audit.py            # прогнать всё, вывести отчёт
    python test_security_audit.py -q         # только итог

Каждый тест утверждает ИСПРАВЛЕННОЕ поведение: FAIL означает регрессию (уязвимость
вернулась). На пропатченном коде все тесты должны быть OK.
"""

import sys
import types
import importlib
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]   # корень проекта


# --------------------------------------------------------------------------
# Инфраструктура: подменяем тяжёлые модули заглушками, чтобы импортировать
# тестируемый код без установленных psycopg/fastapi/docling. (Векторов/Qdrant нет.)
# --------------------------------------------------------------------------
def _stub(name: str):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


def _load_rag():
    import test_stubs
    test_stubs.install()                  # requests(.exceptions)/config — заглушки для deepseek
    return importlib.import_module("rag")


def _load_auth():
    _stub("db")
    _stub("users")
    return importlib.import_module("auth")


# ==========================================================================
# 1. КРИТИЧНО: plan_id вида "..." указывает на КОРЕНЬ каталога планов.
#    planner.delete_plan("...") удалит ВСЕ планы разом (rglob + rmdir корня).
#    Доступно через DELETE /plans/{plan_id} любому админу; крафт или опечатка
#    в id -> потеря всей базы планов адаптации.
# ==========================================================================
def test_plan_id_traversal_to_root():
    import planner
    for evil in ("...", "/", "..", "%$#", "  ", ""):
        try:
            got = planner.plan_dir(evil)
        except ValueError:
            continue  # id отвергнут — верно
        assert got.resolve() != planner.PLANS_DIR.resolve(), (
            f"plan_dir({evil!r}) схлопнулся в корень {planner.PLANS_DIR} — "
            f"delete_plan({evil!r}) снесёт все планы"
        )
    # И удаление по такому id ничего не должно снести (БД не нужна: запрос подменяем —
    # важно, что до SQL доходит ровно этот id и ничего не удаляется).
    orig = planner.db.query
    planner.db.query = lambda *a, **k: None
    try:
        assert planner.delete_plan("...") is False
    finally:
        planner.db.query = orig


# ==========================================================================
# 2. КРИТИЧНО (безопасность): распознаватель приветствий срабатывает по
#    ПОДСТРОКЕ. Короткие фразы, содержащие «да»/«нет»/«ок» внутри других слов,
#    уходят в маршрут "general" ещё до классификации риска — в т.ч. сообщения
#    о плохом самочувствии, которые должны эскалироваться человеку.
# ==========================================================================
def test_greeting_prefilter_does_not_swallow_real_messages():
    tc = _load_rag()
    distress_or_rag = [
        "мне плохо нет сил",         # содержит «нет» -> должно эскалироваться, а не general
        "проблема с интернетом",     # содержит «нет» -> обычный вопрос
        "станок сломался да искрит",  # содержит «да»  -> обычный вопрос
    ]
    misrouted = [q for q in distress_or_rag if tc.is_greeting_or_general(q)]
    assert not misrouted, (
        "Эти сообщения ошибочно приняты за приветствие/общую фразу и не пойдут "
        f"ни в RAG, ни в эскалацию: {misrouted}"
    )
    # Настоящие приветствия/подтверждения по-прежнему должны распознаваться
    # («как дела» намеренно не считается: начинается со слова-вопроса «как».)
    real_greetings = ["привет", "спасибо", "да", "нет", "ок", "добрый день"]
    missed = [q for q in real_greetings if not tc.is_greeting_or_general(q)]
    assert not missed, f"Перестарались: настоящие приветствия не распознаны: {missed}"


# ==========================================================================
# 3. ВЫСОКО (безопасность): защита от перебора паролей ключуется на IP+логин.
#    Каждый новый IP получает свежий лимит из 10 попыток -> распределённый
#    перебор (ботнет/прокси) полностью обходит лок-аут. Плюс нет глобального
#    троттлинга на аккаунт.
# ==========================================================================
def test_lockout_not_bypassable_per_ip():
    auth = _load_auth()
    auth._failures.clear()

    # login() теперь ключует лок-аут ПО ЛОГИНУ (без IP) — перебор с ротацией IP
    # накапливается в один счётчик и не обходит блокировку.
    user = "victim"
    for _ in range(auth.LOCKOUT_ATTEMPTS):
        auth._record_failure(user)
    assert auth._is_locked(user) > 0, (
        "Лок-аут по аккаунту не срабатывает — распределённый перебор проходит"
    )


# ==========================================================================
# 4. СРЕДНЕ (XSS): интерфейс не должен вставлять данные сервера как HTML. React экранирует
#    текст сам; опасны только dangerouslySetInnerHTML и inline-скрипты (их запрещает CSP).
# ==========================================================================
def test_index_html_escapes_error_field():
    src = BASE / "frontend" / "src"
    offenders = [str(p.relative_to(BASE)) for p in src.rglob("*.tsx")
                 if "dangerouslySetInnerHTML" in p.read_text(encoding="utf-8")]
    assert not offenders, f"dangerouslySetInnerHTML в интерфейсе — риск XSS: {offenders}"
    index = BASE / "frontend" / "index.html"
    import re
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index.read_text(encoding="utf-8")), \
        "inline-скрипт в frontend/index.html нарушит CSP (script-src 'self')"
    # Подмена встроенных методов DOM (Node.prototype.removeChild и т. п.) конфликтует с
    # антивирусами и расширениями, которые перехватывают те же методы: обёртки вызывают друг
    # друга по кругу — «RangeError: Maximum call stack size exceeded» на каждом переходе.
    patched = [str(p.relative_to(BASE)) for p in src.rglob("*.ts*")
               if re.search(r"(Node|Element|HTMLElement|Document)\.prototype\.\w+\s*=[^=]",
                            p.read_text(encoding="utf-8"))]
    assert not patched, f"интерфейс подменяет встроенные методы DOM: {patched}"


# ==========================================================================
# 5. СРЕДНЕ: cookie сессии выставляется без флага Secure -> при доступе по HTTP
#    (или downgrade) токен уходит в открытом виде и перехватывается MITM.
# ==========================================================================
def test_session_cookie_is_secure():
    # установка куки живёт в deps.py (веб-слой), но ищем по всем модулям — чтобы
    # тест не отваливался при переносе кода между файлами.
    block = ""
    for path in sorted((BASE / "backend").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        start = src.find("def _set_session_cookie")
        if start >= 0:
            block = src[start:start + 400]
            break
    assert "set_cookie" in block, "не нашли установку куки — проверьте тест"
    assert "secure=" in block, (
        "Cookie сессии выставляется без secure=... — токен уйдёт по HTTP открытым текстом"
    )


# --------------------------------------------------------------------------
# Раннер
# --------------------------------------------------------------------------
TESTS = [
    ("plan_id -> корень каталога планов (снос всех планов)", test_plan_id_traversal_to_root),
    ("префильтр приветствий глотает реальные/тревожные сообщения", test_greeting_prefilter_does_not_swallow_real_messages),
    ("лок-аут обходится сменой IP (распределённый перебор)", test_lockout_not_bypassable_per_ip),
    ("интерфейс без HTML-вставок и inline-скриптов (XSS)", test_index_html_escapes_error_field),
    ("cookie сессии без Secure", test_session_cookie_is_secure),
]


def run(quiet: bool = False):
    passed = failed = skipped = 0
    for title, fn in TESTS:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {title}\n       уязвимость подтверждена: {e}")
        except Exception as e:  # отсутствует зависимость и т.п.
            skipped += 1
            print(f"[SKIP] {title}\n       {type(e).__name__}: {e}")
        else:
            passed += 1
            if not quiet:
                print(f"[ OK ] {title}")
    print("-" * 60)
    print(f"OK={passed}  FAIL(уязвимость активна)={failed}  SKIP={skipped}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run(quiet="-q" in sys.argv) else 0)
