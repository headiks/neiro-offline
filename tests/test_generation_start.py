"""Запуск генерации плана: замок genplan хранит id задачи в поле job_id. Раньше
set_job(ns, job_id, job_id=...) падал TypeError — 500 на «Обновить сообщения плана»
и молчаливый отказ автообновления после загрузки документов. Без Redis/БД."""
import sys
import types

sys.modules.setdefault("indexing", types.SimpleNamespace())
import jobstore  # noqa: E402
import planner  # noqa: E402


def test_start_generation_sets_lock_and_reports_running(monkeypatch):
    monkeypatch.setattr(jobstore, "get_redis", lambda: None)            # in-memory jobstore
    monkeypatch.setattr(planner, "estimate_generation", lambda plan, profs: {"llm_calls": 3, "total": 5})
    queued = []
    monkeypatch.setitem(sys.modules, "jobs", types.SimpleNamespace(enqueue_generation=lambda *a: queued.append(a)))
    job = planner.start_generation({"plan_id": "p-test", "stages": []})
    assert job and job["status"] == "queued" and queued
    lock = jobstore.get_job("genplan", "p-test")
    assert lock["job_id"] == job["job_id"] and lock["dirty"] is False
    assert planner.running_job_for("p-test")["job_id"] == job["job_id"]
    # повторное нажатие — не вторая генерация, а текущая
    assert planner.start_generation({"plan_id": "p-test", "stages": []})["already_running"] is True
    assert len(queued) == 1
