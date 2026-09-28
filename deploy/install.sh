#!/usr/bin/env bash
# Полная установка всего, что нужно для работы RAG-ассистента на Linux-сервере:
#   - системные пакеты (сборка, рендеринг изображений для docling/OCR)
#   - Python-окружение и зависимости проекта (fastapi, docling, ...)
#   - PostgreSQL и Redis в Docker, автозапуск через --restart
#   - systemd-сервис приложения, чтобы всё переживало обрыв SSH и перезагрузку
#
# LLM — облачный DeepSeek (ключ DEEPSEEK_API_KEY в .env.production). Ollama и
# локальные LLM НЕ нужны. Векторного стора (Qdrant) и эмбеддингов тоже нет:
# классификацию документов, поиск и генерацию делает DeepSeek через docpipe
# (LLM-метки этапов/подэтапов в Postgres).
#
# Запускать из корня распакованного проекта (там, где лежат backend/ и data/):
#   chmod +x install.sh && ./install.sh
#
# Работает и от root, и от обычного пользователя с sudo.
set -e

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi
export DEBIAN_FRONTEND=noninteractive
APP_DIR="$(pwd)"
APP_USER="$(whoami)"

echo "==> [1/7] Обновление списка пакетов"
$SUDO apt-get update -y

echo "==> [2/7] Системные зависимости"
# python3-venv/pip — окружение; build-essential — сборка колёс некоторых пакетов;
# libgl1/libglib2.0-0 — нужны OpenCV/EasyOCR, которые тянет docling для разбора PDF;
# curl — установка Docker; ufw — управление файрволом сервера
$SUDO apt-get install -y \
    python3 python3-venv python3-pip \
    build-essential \
    libgl1 libglib2.0-0 \
    curl ufw

echo "==> [3/7] Виртуальное окружение и Python-зависимости"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
# --extra-index-url подтягивает CPU-сборку torch (её тянет docling для layout-моделей
# разбора PDF). Если на сервере есть NVIDIA GPU с CUDA — уберите этот флаг, разбор PDF
# пойдёт на GPU быстрее. Локальных эмбеддингов больше нет — веса моделей не качаем.
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

echo "==> [4/7] Docker (движок для PostgreSQL и Redis)"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | $SUDO sh
fi
# Векторного стора (Qdrant) больше нет — контейнер не поднимаем. Если он остался
# от прошлой установки, его можно удалить: docker rm -f qdrant

echo "==> [5/7] PostgreSQL (аккаунты и структурированные данные)"
# Секреты приложения (DSN с паролем БД, ключ DeepSeek) — в отдельном env-файле,
# он в .gitignore и подключается к systemd-сервису через EnvironmentFile.
ENV_FILE="$APP_DIR/.env.production"
if [ ! -f "$ENV_FILE" ]; then
    PG_PASS="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | xxd -p)"
    cat > "$ENV_FILE" <<EOF
# Секреты приложения — НЕ коммитить (файл в .gitignore)
NEIROMASTER_DB_DSN=postgresql://neiromaster:${PG_PASS}@localhost:5432/neiromaster
# ОБЯЗАТЕЛЬНО впишите ключ DeepSeek (без него генерация/классификация не работают):
DEEPSEEK_API_KEY=
EOF
    chmod 600 "$ENV_FILE"
fi
# Пароль для инициализации контейнера берём из уже записанного DSN (идемпотентно при повторном запуске)
source "$ENV_FILE"
PG_PASS="$(echo "$NEIROMASTER_DB_DSN" | sed -E 's#.*://[^:]+:([^@]+)@.*#\1#')"
# Порт только на 127.0.0.1 — наружу БД не торчит. Данные в томе pg_data (переживают пересоздание контейнера).
if ! $SUDO docker ps -a --format '{{.Names}}' | grep -qx neiromaster-pg; then
    $SUDO docker run -d --name neiromaster-pg --restart unless-stopped \
        -e POSTGRES_USER=neiromaster -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=neiromaster \
        -p 127.0.0.1:5432:5432 \
        -v "$APP_DIR/pg_data:/var/lib/postgresql/data" \
        postgres:16-alpine -c max_connections=300
fi
echo "    Ожидание готовности PostgreSQL..."
for i in $(seq 1 60); do
    $SUDO docker exec neiromaster-pg pg_isready -U neiromaster >/dev/null 2>&1 && break
    sleep 1
done

# Redis — брокер очереди задач + общий стейт (прогресс/отмена, история диалогов,
# глобальный лимитер DeepSeek). Нужен для высоконагруженного режима (несколько
# web-воркеров + отдельные worker-процессы). Только на localhost.
echo "==> Redis (очередь задач и общий стейт)"
if ! $SUDO docker ps -a --format '{{.Names}}' | grep -qx neiromaster-redis; then
    $SUDO docker run -d --name neiromaster-redis --restart unless-stopped \
        -p 127.0.0.1:6379:6379 redis:7-alpine
fi

# Число web-воркеров задаётся переменной WEB_CONCURRENCY в env-файле (gunicorn читает
# её сам) — так его крутят без правки юнита. Дефолт gunicorn (если не задано) = 1,
# поэтому в .env.production держи WEB_CONCURRENCY=6 (см. .env.example).
WEB_WORKERS="${NEIROMASTER_WEB_WORKERS:-6}"     # только для текста подсказки ниже
RQ_WORKERS="${NEIROMASTER_RQ_WORKERS:-4}"       # worker-процессы под тяжёлые задачи
# Сайт слушает только localhost: наружу его отдаёт HTTPS-прокси (Caddy/nginx). Открыть
# порт напрямую (без TLS) — NEIROMASTER_BIND=0.0.0.0:8000 ./install.sh (не рекомендуется).
BIND="${NEIROMASTER_BIND:-127.0.0.1:8000}"

echo "==> [6/7] systemd-сервисы: web (gunicorn) + worker (RQ)"
cat <<EOF | $SUDO tee /etc/systemd/system/rag-app.service > /dev/null
[Unit]
Description=RAG Assistant web (gunicorn+uvicorn, DeepSeek)
After=network.target docker.service
Requires=docker.service

[Service]
WorkingDirectory=$APP_DIR
# DSN к PostgreSQL, ключ DeepSeek и REDIS_URL — из защищённого env-файла (chmod 600)
EnvironmentFile=$ENV_FILE
# Несколько uvicorn-воркеров = процессная многопоточность web-тира под нагрузку.
# -w НЕ задаём: gunicorn берёт число воркеров из WEB_CONCURRENCY (env-файл) — тюним без правки юнита.
# -b 127.0.0.1: снаружи сайт доступен только через HTTPS-прокси (кука сессии Secure).
# --forwarded-allow-ips: прокси на этом же хосте — доверяем его X-Forwarded-For (реальный IP
# клиента нужен лимитам входа и журналу действий).
Environment=PYTHONPATH=$APP_DIR/backend
ExecStart=$APP_DIR/.venv/bin/gunicorn app:app \\
    -k uvicorn.workers.UvicornWorker \\
    -b $BIND --timeout 120 --graceful-timeout 30 --forwarded-allow-ips=127.0.0.1
Restart=on-failure
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF

# Worker-тир: @-инстансы (rag-worker@1, rag-worker@2, …) — сколько нужно пропускной
# способности под классификацию/генерацию. Все берут задачи из общей очереди Redis.
cat <<EOF | $SUDO tee /etc/systemd/system/rag-worker@.service > /dev/null
[Unit]
Description=RAG Assistant worker %i (RQ: классификация/генерация)
After=network.target docker.service rag-app.service
Requires=docker.service

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$APP_DIR/backend
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/backend/worker.py
Restart=on-failure
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable rag-app
for i in $(seq 1 "$RQ_WORKERS"); do $SUDO systemctl enable "rag-worker@$i"; done

echo ""
echo "==> [7/7] Проверка сервисов"
$SUDO docker exec neiromaster-pg pg_isready -U neiromaster >/dev/null 2>&1 && echo "    Postgres OK (127.0.0.1:5432)" || echo "    Postgres НЕ отвечает"
$SUDO docker exec neiromaster-redis redis-cli ping 2>/dev/null | grep -qi pong && echo "    Redis   OK (127.0.0.1:6379)" || echo "    Redis   НЕ отвечает"
if grep -q '^DEEPSEEK_API_KEY=.\+' "$ENV_FILE"; then
    echo "    DeepSeek ключ  задан"
else
    echo "    ВНИМАНИЕ: DEEPSEEK_API_KEY пуст в $ENV_FILE — впишите ключ до запуска!"
fi

echo ""
echo "==> Готово."
echo ""
echo "1) Впишите ключ DeepSeek в $ENV_FILE (строка DEEPSEEK_API_KEY=...)."
echo "2) Документы загружаются и размечаются через веб-интерфейс (docling + DeepSeek,"
echo "   метки этапов/подэтапов в Postgres). Отдельный шаг индексации не нужен."
echo ""
echo "Запуск web + worker'ов как systemd-сервисов (переживут reboot и разрыв SSH):"
echo "    sudo systemctl start rag-app                       # web (gunicorn, $WEB_WORKERS воркеров)"
echo "    for i in \$(seq 1 $RQ_WORKERS); do sudo systemctl start rag-worker@\$i; done   # worker-тир"
echo "    sudo systemctl status rag-app 'rag-worker@*'"
echo "    sudo journalctl -u rag-app -u 'rag-worker@*' -f    # логи web+worker"
echo ""
echo "Масштаб под нагрузку: web-воркеры — WEB_CONCURRENCY в env-файле, число worker-процессов —"
echo "запуском новых rag-worker@N. Кап одновременных вызовов DeepSeek — DEEPSEEK_MAX_CONCURRENCY."
echo ""
echo "Сайт слушает $BIND. Наружу его отдаёт HTTPS-прокси, например Caddy (сертификат сам):"
echo "    sudo apt install caddy"
echo "    echo 'ваш-домен.ru { reverse_proxy 127.0.0.1:8000 }' | sudo tee /etc/caddy/Caddyfile"
echo "    sudo systemctl reload caddy && sudo ufw allow 80,443/tcp"
echo "Проверка живости: curl -s http://$BIND/healthz   -> {\"ok\":true,...}"
echo "Порты Postgres (5432), Redis (6379) и приложения (8000) — только localhost, наружу не открывать."
echo "Чек-лист выкатки: docs/production.md"
