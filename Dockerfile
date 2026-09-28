# syntax=docker/dockerfile:1
#
# Образы НейроМастера. Собирать и запускать — одной командой из корня репозитория:
#     docker compose up -d
#
# Цели сборки (target):
#   web    — API и сайт: gunicorn + uvicorn. Без docling: разбор документов делает worker.
#   worker — очередь RQ: разбор документов (docling + torch CPU) и генерация через DeepSeek.

ARG PYTHON_VERSION=3.11

# ---------- Сайт: React-сборка -> static/app (Node нужен только здесь) ----------
FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/
RUN npm ci --prefix frontend --no-audit --no-fund
COPY shared/ shared/
COPY frontend/ frontend/
RUN npm run build --prefix frontend


# ---------- Общая часть Python ----------
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120
# PIP_DEFAULT_TIMEOUT: torch и docling — сотни МБ; на медленном канале/VPN дефолтные 15 с
# обрывают сборку по таймауту чтения.
# Процесс приложения — не root. Каталоги данных создаются заранее с нужным владельцем:
# именованный том при первом подключении наследует владельца каталога из образа.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app app
WORKDIR /app
COPY requirements-web.txt .
RUN pip install -r requirements-web.txt


# ---------- web ----------
FROM base AS web
COPY --chown=app:app . .
COPY --from=frontend --chown=app:app /src/static/app static/app
RUN mkdir -p data/documents data/converted data/processed data/secrets data/app \
 && chown app:app . && chown -R app:app data
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
# Код — в backend/ (PYTHONPATH), рабочая папка — корень: data/ и static/ от неё.
# Число процессов — WEB_CONCURRENCY (gunicorn читает сам). Порт web снаружи не публикуется —
# перед ним Caddy, поэтому заголовкам X-Forwarded-* из внутренней сети доверяем.
CMD ["gunicorn", "app:app", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", \
     "--timeout", "120", "--graceful-timeout", "30", "--forwarded-allow-ips", "*"]


# ---------- worker ----------
FROM base AS worker
# torch — CPU-сборка: без CUDA образ меньше на несколько ГБ, GPU разбору документов не нужен.
# Ставим до docling, чтобы он не притянул CUDA-вариант с PyPI.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=app:app . .
# Модели docling/EasyOCR скачиваются при первом разборе в /app/.cache (том в compose) —
# один раз, а не при каждом пересоздании контейнера.
# ponytail: модели не запечены в образ — worker'у нужен интернет к HuggingFace при первом
# разборе. Для полностью офлайн-обработки: `docling-tools models download` на этапе сборки.
ENV HF_HOME=/app/.cache/huggingface \
    EASYOCR_MODULE_PATH=/app/.cache/easyocr
RUN mkdir -p data/documents data/converted data/processed data/secrets .cache \
 && chown -R app:app data .cache
USER app
CMD ["python", "backend/worker.py"]
