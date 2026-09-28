"""Удаление нескольких документов: права — как у одиночного, чужие/несуществующие — в
skipped с причиной, пересчёт сообщений запускается один раз на всю пачку."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api_documents
import deps


def test_bulk_delete(monkeypatch):
    store = {"a.pdf", "b.docx", "c.pdf"}
    monkeypatch.setattr(api_documents.indexing, "delete_document", lambda n: n in store and not store.discard(n))
    monkeypatch.setattr(api_documents.documents, "remove_by_filename", lambda n: None)

    def access(user, name, write=False):
        if name == "c.pdf":
            raise HTTPException(status_code=403, detail="Документ загружен другим администратором")
    monkeypatch.setattr(api_documents, "ensure_doc_access", access)
    bg = []
    monkeypatch.setattr(api_documents, "_bg", lambda fn, *a: bg.append(fn))
    monkeypatch.setattr(api_documents.activitylog, "log", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(api_documents.router)
    app.dependency_overrides[deps.require_admin] = lambda: {"id": "adm", "role": "admin"}
    r = TestClient(app).post("/documents/bulk-delete", json={"filenames": ["a.pdf", "b.docx", "a.pdf", "c.pdf", "x.pdf"]})
    assert r.status_code == 200
    d = r.json()
    assert d["deleted"] == 2 and store == {"c.pdf"}
    assert {s["filename"]: s["reason"] for s in d["skipped"]} == {
        "c.pdf": "Документ загружен другим администратором", "x.pdf": "Документ не найден"}
    assert len(bg) == 1                                   # один пересчёт на пачку
