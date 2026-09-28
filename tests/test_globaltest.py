"""Раздел /globaltest: тесты и диагностика только после пароля раздела (хеш в env), кука
подписана и привязана к пользователю; без пароля API тестов отвечает 403."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_pages
import api_people
import deps
import security
import users

ADMIN = {"id": "adm-1", "role": "admin", "must_change_credentials": False}


def _client(monkeypatch):
    salt, digest = users.hash_password("Ytest-pass-123")
    monkeypatch.setenv("NEIROMASTER_GLOBALTEST_PASSWORD_HASH", f"{salt}:{digest}")
    monkeypatch.setattr(api_pages.activitylog, "log", lambda *a, **k: None)
    monkeypatch.setattr(deps.auth, "COOKIE_SECURE", False)
    monkeypatch.setattr(api_pages.auth, "COOKIE_SECURE", False)
    security.reset("globaltest")
    monkeypatch.setattr(api_people.messaging, "test_samples", lambda: [{"kind": "message"}], raising=False)
    app = FastAPI()
    app.include_router(api_pages.router)
    app.include_router(api_people.router)
    app.dependency_overrides[deps.require_admin] = lambda: ADMIN
    return TestClient(app)


def test_unlock_flow(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/globaltest").json() == {"configured": True, "unlocked": False}
    assert c.get("/test-messages/samples").status_code == 403            # тест закрыт
    assert c.post("/api/globaltest/unlock", json={"password": "wrong"}).status_code == 403
    r = c.post("/api/globaltest/unlock", json={"password": "Ytest-pass-123"})
    assert r.status_code == 200 and deps.GT_COOKIE in r.cookies
    assert c.get("/api/globaltest").json()["unlocked"] is True
    assert c.get("/test-messages/samples").status_code == 200            # теперь открыт
    c.post("/api/globaltest/lock")
    assert c.get("/test-messages/samples").status_code == 403


def test_token_bound_to_user_and_expires(monkeypatch):
    _client(monkeypatch)

    class Req:
        def __init__(self, tok): self.cookies = {deps.GT_COOKIE: tok}
    tok = deps.gt_token("adm-1")
    assert deps.gt_unlocked(Req(tok), ADMIN)
    assert not deps.gt_unlocked(Req(tok), {"id": "other"})             # чужая кука не подходит
    assert not deps.gt_unlocked(Req(deps.gt_token("adm-1", now=1)), ADMIN)   # истёкшая
    monkeypatch.setenv("NEIROMASTER_GLOBALTEST_PASSWORD_HASH", "")      # пароль не настроен — закрыто
    assert not deps.gt_unlocked(Req(tok), ADMIN)


def test_brute_force_limited(monkeypatch):
    c = _client(monkeypatch)
    codes = [c.post("/api/globaltest/unlock", json={"password": "x"}).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
