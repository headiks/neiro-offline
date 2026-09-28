# Настройки gunicorn — он сам читает этот файл из рабочей папки (корень проекта).
# Число процессов — по железу сервера (backend/sizing.py), WEB_CONCURRENCY — ручная замена.
import sys

sys.path.insert(0, "backend")
import sizing  # noqa: E402

workers = sizing.web_workers()
worker_class = "uvicorn.workers.UvicornWorker"
bind = "0.0.0.0:8000"
timeout = 120
graceful_timeout = 30
# Дольше, чем Caddy держит простаивающее соединение (2 мин): иначе под нагрузкой приложение
# закрывает соединение, которое Caddy как раз переиспользует, — 502 (стресс-тест).
keepalive = 150
# Порт web снаружи не публикуется — перед ним Caddy, заголовкам X-Forwarded-* доверяем.
forwarded_allow_ips = "*"

print(f"[sizing] {sizing.summary()}", flush=True)
