"""Страница «Приложение»: сведения об APK и отдача файла; без файла — понятный 404."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_pages



def _client():
    app = FastAPI()
    app.include_router(api_pages.router)
    return TestClient(app)


def test_apk_missing_and_present(tmp_path, monkeypatch):
    apk = tmp_path / "NeiroMaster.apk"
    monkeypatch.setenv("NEIROMASTER_APK", str(apk))
    c = _client()
    assert c.get("/api/app/android").json() == {"available": False}
    assert c.get("/app/android.apk").status_code == 404
    apk.write_bytes(b"PK\x03\x04apk")
    info = c.get("/api/app/android").json()
    assert info["available"] and info["size"] == 7 and info["url"] == "/app/android.apk"
    r = c.get("/app/android.apk")
    assert r.status_code == 200 and r.content == b"PK\x03\x04apk"
    assert r.headers["content-type"] == "application/vnd.android.package-archive"
    assert "NeiroMaster.apk" in r.headers["content-disposition"]
