"""
E2E-проверка нового сайта в настоящем браузере (Playwright + Chromium): первый вход,
разделы админки, карточка сотрудника, тур «Как пользоваться», служебные страницы,
заголовки безопасности, кабинет сотрудника на телефоне и на компьютере. Ловит ошибки JS
в консоли и бесконечные циклы запросов, делает скриншоты.

Нужен запущенный сервер на ЧИСТОЙ базе с известным начальным паролем владельца:
    NEIROMASTER_ADMIN_PASSWORD=Owner-start-2026 NEIROMASTER_INSECURE_COOKIE=1 uvicorn app:app
Затем:
    E2E_BASE=http://127.0.0.1:8000 E2E_OWNER_PASSWORD=Owner-start-2026 python e2e_test.py
Скриншоты — в E2E_SHOTS (по умолчанию ./e2e_shots). Браузер — Chromium из Playwright или
путь в E2E_CHROMIUM. Модель DeepSeek не нужна.
"""
import collections
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8000")
OWNER_START = os.environ.get("E2E_OWNER_PASSWORD", "Owner-start-2026")
OWNER_PASS = "Owner-e2e-pass-2026"
SHOTS = Path(os.environ.get("E2E_SHOTS", "e2e_shots"))
CHROMIUM = os.environ.get("E2E_CHROMIUM") or None

errors: list = []
checks: list = []
SEEN = "localStorage.setItem('nm_tour_seen_admin','1');localStorage.setItem('nm_tour_seen_cabinet','1');"


def check(cond, what):
    checks.append((bool(cond), what))
    print(("  ok   " if cond else "  FAIL ") + what)


def watch(page, label):
    page.on("console", lambda m: m.type == "error" and errors.append(f"[{label}] console: {m.text}"))
    page.on("pageerror", lambda e: errors.append(f"[{label}] pageerror: {e}"))


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def owner_username():
    import psycopg
    dsn = os.environ.get("NEIROMASTER_DB_DSN", "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_dev")
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT username FROM users WHERE role='owner' ORDER BY created_at LIMIT 1").fetchone()[0]


def api(page, method, url, body=None):
    """Запрос из страницы (кука сессии + свой Origin)."""
    return page.evaluate("""async ([m, u, b]) => {
        const r = await fetch(u, {method: m, headers: {'Content-Type': 'application/json'},
                                  body: b === null ? undefined : JSON.stringify(b)});
        let d = null; try { d = await r.json(); } catch (e) {}
        return {status: r.status, data: d};
    }""", [method, url, body])


def login(page, username, password):
    page.goto(f"{BASE}/login")
    page.fill("input[autocomplete=username]", username)
    page.fill("input[autocomplete=current-password]", password)
    with page.expect_navigation(timeout=15000):
        page.click("button[type=submit]")
    page.wait_for_load_state("load")
    page.wait_for_timeout(900)


def setup_password(page, password):
    pw = page.locator("input[type=password]")
    pw.nth(0).fill(password)
    pw.nth(1).fill(password)
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r".*/login$"), timeout=15000)


def open_tour(page):
    page.click("[data-tour=profile-menu]")
    page.get_by_role("menuitem", name="Как пользоваться").click()


def tour_run(page, label, expect_min, start):
    """Проходит тур кнопкой «Далее», проверяя, что каждый шаг показан, и закрывает его."""
    start()
    page.wait_for_selector(".nmt-card.nmt-in", timeout=8000)
    total = int(re.search(r"из (\d+)", page.inner_text(".nmt-count")).group(1))
    check(total >= expect_min, f"{label}: в туре {total} шагов (ожидалось ≥ {expect_min})")
    titles = []
    for i in range(total):
        page.wait_for_function("() => document.querySelector('.nmt-card.nmt-in')", timeout=10000)
        title = page.inner_text(".nmt-title")
        titles.append(title)
        if title in ("Карточка сотрудника", "План и дата выхода"):
            page.wait_for_timeout(400)
            check(page.locator("dialog.nmt-demo[open]").count() == 1, f"{label}: «{title}» — карточка сотрудника открыта")
        if title in ("Название плана", "Этапы"):
            page.wait_for_timeout(400)
            check(page.locator(".nm-stage").count() >= 1, f"{label}: «{title}» — демо-план в редакторе")
        if title == "Напишите вопрос своими словами":
            page.wait_for_timeout(300)
            check(page.is_visible("#nm-question"), f"{label}: поле вопроса видно")
        if title == "Отправить":
            check(page.input_value("#nm-question") != "", f"{label}: тур напечатал пример вопроса")
        if i in (0, 4, total // 2, total - 1):
            time.sleep(0.9)
            shot(page, f"{label}-tour-{i + 1:02d}")
        if i < total - 1:
            before = page.inner_text(".nmt-count")
            page.click('.nmt-card [data-act="next"]')
            page.wait_for_function("(c) => document.querySelector('.nmt-count').textContent !== c "
                                   "&& document.querySelector('.nmt-card.nmt-in')", arg=before, timeout=15000)
    check(len(set(titles)) == total, f"{label}: все {total} шагов показаны без повторов")
    page.click('.nmt-card [data-act="next"]')          # «Готово ✓» закрывает тур
    page.wait_for_function("() => document.querySelector('.nmt-root').hidden", timeout=5000)
    page.wait_for_timeout(600)
    return titles


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)

        # ---------------------------------------------------------- владелец
        ctx = browser.new_context(viewport={"width": 1366, "height": 860}, locale="ru-RU")
        ctx.add_init_script(SEEN)
        page = ctx.new_page()
        watch(page, "admin")
        requests = collections.Counter()
        page.on("request", lambda r: requests.update([r.url.split("?")[0].replace(BASE, "")]))
        print("Вход владельца и первичная настройка")
        login(page, owner_username(), OWNER_START)
        check(page.url.endswith("/setup"), "первый вход ведёт на /setup")
        setup_password(page, OWNER_PASS)
        check(True, "свой пароль задан, сессия сброшена — снова вход")
        login(page, owner_username(), OWNER_PASS)
        check(page.url.endswith("/admin/users"), "после смены пароля — админка (/admin/users)")

        print("Подготовка данных через API")
        plan = api(page, "POST", "/plans/template")["data"]
        check(plan and plan.get("plan_id"), "стандартный план создан")
        today = time.strftime("%Y-%m-%d")
        e1 = api(page, "POST", "/users", {"full_name": "Петров Пётр Петрович", "position": "Оператор",
                                          "department": "Цех 1", "plan_id": plan["plan_id"], "start_date": today})["data"]
        api(page, "POST", "/users", {"full_name": "Сидорова Анна Сергеевна", "position": "Мастер", "department": "Цех 1"})
        r = api(page, "POST", "/users/" + e1["id"] + "/test-messages", {})
        check(r["status"] == 200, "тестовые сообщения всех типов отправлены сотруднику")

        print("Разделы админки")
        requests.clear()
        for nav, heading in (("nav-users", "Пользователи"), ("nav-plans", "Планы адаптации"), ("nav-documents", "Документы"),
                             ("nav-messages", "Сообщения сотрудникам"), ("nav-questions", "Вопросы сотрудников")):
            page.click(f"[data-tour={nav}]")
            try:        # переход по меню — без перезагрузки вкладки, страница появляется сама
                page.get_by_role("heading", name=heading).first.wait_for(timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(500)
            check(page.get_by_role("heading", name=heading).count() >= 1, f"раздел «{heading}» открывается по клику в меню")
            shot(page, f"admin-{nav}")
        worst = max(requests.values()) if requests else 0
        check(worst < 15, f"нет циклов запросов (максимум {worst} одинаковых запросов за обход разделов)")

        page.click("[data-tour=nav-users]")
        page.wait_for_timeout(800)
        rows = page.locator(".nm-table-body .nm-table-row").count()
        check(rows >= 3, f"в списке пользователей есть строки ({rows})")
        page.fill("input[aria-label='Поиск']", "Сидорова")
        page.wait_for_timeout(300)
        check(page.locator(".nm-table-body .nm-table-row").count() == 1, "поиск по ФИО фильтрует список")
        page.fill("input[aria-label='Поиск']", "")

        print("Карточка сотрудника: создание через интерфейс")
        page.click("[data-tour=btn-add-user]")
        page.wait_for_selector("dialog[open]")
        page.fill("[data-tour=emp-name] input", "Кузнецова Ольга Игоревна")
        page.get_by_role("button", name="Сохранить").click()
        page.wait_for_selector("text=Сотрудник создан", timeout=8000)
        check(page.locator("dialog[open] code").count() >= 1, "после создания показан временный пароль")
        shot(page, "admin-user-created")
        page.get_by_role("button", name="Готово").click()
        page.wait_for_timeout(600)
        check(page.locator(".nm-table-body .nm-table-row").count() == rows + 1, "новый сотрудник в списке")

        print("Конструктор плана")
        page.click("[data-tour=nav-plans]")
        page.wait_for_timeout(900)
        page.select_option("[data-tour=plan-select] select", plan["plan_id"])
        page.wait_for_timeout(900)
        stages_before = page.locator(".nm-stage").count()
        check(stages_before == 8, f"стандартный план: 8 этапов в редакторе (есть {stages_before})")
        title_before = page.input_value("[data-tour=plan-title] input")
        shot(page, "admin-plan-editor")

        print("Тур по админке")
        titles = tour_run(page, "admin", 25, lambda: open_tour(page))
        check(any("С чего начать" in t for t in titles), "тур показывает выбор плана")
        check(page.url.endswith("/admin/plans"), "после тура — тот же раздел")
        check(page.locator("dialog[open]").count() == 0, "окно карточки закрыто после тура")
        check(page.input_value("[data-tour=plan-title] input") == title_before, "редактируемый план вернулся после тура")
        check(page.locator(".nm-stage").count() == stages_before, "этапы плана не изменились")
        plans_after = api(page, "GET", "/plans")["data"]["plans"]
        check(len(plans_after) == 1, "тур ничего не сохранил (план один)")

        print("Автопросмотр и Esc")
        open_tour(page)
        page.wait_for_selector(".nmt-card.nmt-in", timeout=8000)
        first = page.inner_text(".nmt-count")
        page.wait_for_function("(c) => document.querySelector('.nmt-count').textContent !== c", arg=first, timeout=25000)
        check(True, f"автопросмотр сам перешёл к следующему шагу ({first} → {page.inner_text('.nmt-count')})")
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.querySelector('.nmt-root').hidden", timeout=5000)
        check(True, "Esc закрывает тур")

        print("Меню настроек")
        gear = page.locator("[data-tour=profile-menu]")
        check(gear.get_attribute("aria-label") == "Настройки", "у профиля — кнопка «Настройки» (шестерёнка)")
        gear.click()
        page.wait_for_selector(".nm-menu")
        mb = page.locator(".nm-menu").bounding_box()
        pb = page.locator(".nm-profile").bounding_box()
        check(mb["x"] >= 0 and mb["y"] >= 0 and mb["x"] + mb["width"] <= 1366 and mb["y"] + mb["height"] <= 860,
              "меню настроек целиком в окне")
        check(mb["y"] + mb["height"] <= pb["y"] + 1, "меню открывается над карточкой профиля, не перекрывая её")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check(page.locator(".nm-menu").count() == 0 and page.evaluate("document.activeElement.dataset.tour") == "profile-menu",
              "Esc закрывает меню, фокус — на кнопке")

        print("Тема")
        page.click("[data-tour=profile-menu]")
        page.get_by_role("menuitem", name="Тёмная тема").click()
        check(page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark", "тёмная тема включается из меню профиля")
        shot(page, "admin-dark")
        page.click("[data-tour=profile-menu]")
        page.get_by_role("menuitem", name="Светлая тема").click()

        print("Служебные страницы")
        for path in ("/logs", "/plans-db", "/notify-test", "/queue-test", "/s3", "/documents-table",
                     "/documents-board", "/doc-breakdown", "/globaltest", "/message-test"):
            before = len(errors)
            resp = page.goto(BASE + path)
            page.wait_for_timeout(900)
            check(resp.status == 200 and len(errors) == before and page.locator("h1").count() >= 1,
                  f"{path} открывается без ошибок JS")
        hdr = page.goto(BASE + "/admin/users").headers.get("content-security-policy", "")
        check("frame-ancestors 'none'" in hdr and "script-src 'self';" in hdr, "CSP без inline-скриптов на страницах")

        creds = api(page, "POST", f"/users/{e1['id']}/credentials", {"password": None})
        check(creds["status"] == 200, "администратор выдал сотруднику пароль")
        emp_login, emp_tmp = creds["data"]["username"], creds["data"]["temp_password"]
        ctx.close()

        # ---------------------------------------------------------- сотрудник, телефон
        print("Кабинет сотрудника на телефоне")
        m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                has_touch=True, locale="ru-RU")
        m.add_init_script(SEEN)
        mp = m.new_page()
        watch(mp, "mobile")
        login(mp, emp_login, emp_tmp)
        check(mp.url.rstrip("/") == BASE, "сотрудник входит по выданному паролю и попадает в кабинет")
        check(mp.locator("[data-tour=tabbar]").is_visible(), "на телефоне — таб-бар как в приложении")
        mp.wait_for_timeout(800)
        cards = mp.locator("[data-tour=today] article")
        check(cards.count() >= 5, f"в чате сегодняшние сообщения ({cards.count()})")
        times = mp.locator("[data-tour=today] article time").all_inner_texts()
        check(times == sorted(times), "сообщения: старые сверху, новые снизу")
        gap = mp.evaluate("document.documentElement.scrollHeight - window.scrollY - window.innerHeight")
        check(gap < 60, "лента прокручена к последнему сообщению")
        shot(mp, "mobile-chat")
        box = mp.locator("[data-tour=today] [role=checkbox]").first
        box.click()
        mp.wait_for_timeout(700)
        check(box.get_attribute("aria-checked") == "true", "пункт чек-листа отмечается")
        mp.reload()
        mp.wait_for_timeout(1500)
        check(mp.locator("[data-tour=today] [role=checkbox][aria-checked=true]").count() >= 1, "отметка сохранилась на сервере")
        for tab in ("history", "ask", "settings", "chat"):
            mp.click(f"[data-tour=tab-{tab}]")
            mp.wait_for_timeout(500)
            check(mp.locator(f"[data-tour=tab-{tab}][aria-current=page]").count() == 1, f"вкладка «{tab}» открывается")
            if tab != "chat":
                shot(mp, f"mobile-{tab}")
        mp.click("[data-tour=tab-settings]")
        mp.wait_for_timeout(400)
        mp.get_by_role("switch", name="Я на больничном").click()
        mp.wait_for_timeout(900)
        check(mp.get_by_role("switch", name="Я на больничном").get_attribute("aria-checked") == "true", "больничный включается")
        mp.click("[data-tour=tab-chat]")
        mp.wait_for_timeout(500)
        check(mp.locator(".nm-banner").count() == 1, "на «Чате» — баннер больничного")
        check("Пауза" in mp.inner_text("[data-tour=progress]"), "на больничном прогресс показывает паузу плана")
        mp.get_by_role("button", name="Снять больничный").click()
        mp.wait_for_timeout(900)
        check(mp.locator(".nm-banner").count() == 0, "больничный снят")
        check("Пауза" not in mp.inner_text("[data-tour=progress]"), "после выхода план снова идёт")

        print("Тур по кабинету (телефон)")
        mp.click("[data-tour=tab-settings]")
        mp.wait_for_timeout(400)
        tour_run(mp, "mobile", 10, lambda: mp.get_by_role("button", name="Как пользоваться").click())
        check(mp.locator("[data-tour=tab-settings][aria-current=page]").count() == 1, "после тура — та же вкладка")
        mp.click("[data-tour=tab-ask]")
        mp.wait_for_timeout(400)
        check(mp.input_value("#nm-question") == "", "напечатанный туром вопрос стёрт")
        overflow = mp.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        check(overflow <= 1, "нет горизонтальной прокрутки на телефоне")
        mp.click("[data-tour=tab-settings]")
        mp.get_by_role("button", name="Как пользоваться").click()
        mp.wait_for_selector(".nmt-card.nmt-in", timeout=8000)
        rect = mp.evaluate("(() => { const r = document.querySelector('.nmt-card').getBoundingClientRect(); return [r.left, r.right, r.bottom]; })()")
        check(rect[0] >= 0 and rect[1] <= 390 and rect[2] <= 844, "карточка тура помещается в экран телефона")
        mp.keyboard.press("Escape")
        m.close()

        # ---------------------------------------------------------- сотрудник, компьютер
        print("Кабинет сотрудника на компьютере")
        d = browser.new_context(viewport={"width": 1366, "height": 860}, locale="ru-RU")
        d.add_init_script(SEEN)
        dp = d.new_page()
        watch(dp, "desktop-employee")
        login(dp, emp_login, emp_tmp)
        check(dp.locator("[data-tour=progress]").is_visible(), "карточка прогресса адаптации")
        check("День 1 из" in dp.inner_text("[data-tour=progress]"), "прогресс: «День 1 из N» в день выхода")
        check(dp.locator("[data-tour=assistant] #nm-question").is_visible(), "ассистент со строкой вопроса")
        pending = []
        dp.route("**/ask", lambda route: pending.append(route))      # ответ «думает», пока не отпустим
        dp.fill("#nm-question", "Что положено из спецодежды?")
        dp.keyboard.press("Enter")
        dp.wait_for_selector(".nm-typing")
        jumps = 0
        for _ in range(20):
            jumps += dp.evaluate("(() => { const l = document.querySelector('.nm-chat-log');"
                                 " return l.scrollHeight > l.clientHeight || l.scrollWidth > l.clientWidth ? 1 : 0; })()")
            dp.wait_for_timeout(50)
        check(jumps == 0, "пока ассистент ищет ответ, полоса прокрутки не мигает")
        for route in pending:
            route.fulfill(status=200, json={"question": "Что положено из спецодежды?", "session_id": "e2e", "route": "rag",
                                            "answer": "Спецодежду выдают в первый день.", "sources": []})
        dp.wait_for_selector("text=Спецодежду выдают в первый день.")
        dp.unroute("**/ask")
        check(dp.locator("[data-tour=admin-nav]").count() == 0, "сотруднику не видны разделы администратора")
        r = dp.goto(BASE + "/admin/users")
        dp.wait_for_timeout(500)
        check(dp.url.rstrip("/") == BASE, "админка сотруднику недоступна (редирект в кабинет)")
        shot(dp, "desktop-cabinet")
        tour_run(dp, "cabinet", 8, lambda: (dp.click("[data-tour=profile-menu]"), dp.get_by_role("menuitem", name="Как пользоваться").click()))
        d.close()
        browser.close()

    failed = [w for ok, w in checks if not ok]
    print(f"\nПроверок: {len(checks)}, упало: {len(failed)}; ошибок JS: {len(errors)}")
    for e in errors[:20]:
        print("  JS:", e)
    if failed or errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
