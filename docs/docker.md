# НейроМастер в Docker

## Запуск — одна команда

```bash
docker compose up -d
```

Нужен только Docker (Compose v2.24+). Node.js, Python, PostgreSQL на машине не нужны —
всё собирается и работает в контейнерах. Первый запуск собирает образы (worker с docling —
дольше всего), дальше старт занимает секунды.

При первом старте само создаётся:

- схема обеих БД и миграции;
- ключ шифрования ПДн (том `secrets`);
- учётка главного администратора — логин и пароль в логе:

```bash
docker compose logs web | grep -A3 "главного администратора"
```

Сайт: **https://localhost** (или адрес из `SITE_ADDRESS`). Для `localhost` и IP-адреса
сертификат выпускает внутренний CA Caddy — браузер один раз предупредит, это ожидаемо.

## Что внутри

| Контейнер | Назначение |
|---|---|
| `caddy` | вход снаружи: HTTPS (порты 80/443) → `web`. Единственный с портами наружу |
| `web` | API и сайт (gunicorn + uvicorn). При старте — схема БД и миграции |
| `worker` | разбор документов (docling) и генерация через DeepSeek, очередь RQ. Процессов внутри — по железу |
| `db-processed` | основная БД: всё, что получено обработкой (разделы, разметка, планы, сообщения), и рабочие данные (пользователи, сессии, журнал) |
| `db-raw` | исходники: оригиналы документов и загруженные штатки — байт в байт. Штатки зашифрованы ключом ПДн |
| `redis` | очередь задач и общий стейт процессов |
| `embed` | векторы вопросов для базы готовых ответов: bge-m3 на CPU (llama.cpp). Модель ~600 МБ скачивается при первом старте в том `models-llm`, дальше работает без интернета. Недоступен — база ответов ищет только точные совпадения |

Из `db-raw` всегда можно заново получить содержимое `db-processed` (переобработка), поэтому
`db-processed` — то, что переносится в закрытый контур, а `db-raw` может остаться в контуре
обработки.

## Настройки

Необязательный файл `.env` рядом с `docker-compose.yml` (образец — `.env.example`). Без него
всё стартует со значениями по умолчанию. Основное:

| Переменная | По умолчанию | Что это |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | ключ модели; без него разбор документов и ответы ассистента не работают |
| `SITE_ADDRESS` | `localhost` | домен (сертификат Let's Encrypt, нужен интернет) или IP (внутренний CA) |
| `POSTGRES_PROCESSED_PASSWORD`, `POSTGRES_RAW_PASSWORD` | `neiromaster`, `neiromaster_raw` | пароли БД. **Задайте до первого запуска** — потом их меняют через `ALTER USER` |
| `NEIROMASTER_ADMIN_PASSWORD` | сгенерируется | начальный пароль владельца, от 8 символов |
| `WEB_CONCURRENCY`, `NEIROMASTER_RQ_WORKERS`, `NEIROMASTER_DB_POOL`, `NEIROMASTER_PUSH_WORKERS` | по железу | процессов web, RQ-воркеров, пул БД, потоков отправки пушей. Сервер считает сам по ядрам и памяти (`backend/sizing.py`, итог — в логе `[sizing]`); задавайте, только чтобы переопределить |
| `HTTP_PORT`, `HTTPS_PORT` | `80`, `443` | порты на хосте |

БД и Redis наружу не публикуются — доступны только контейнерам приложения.

## Обновление

```bash
git pull && docker compose up -d --build
```

Миграции схемы применяются сами при старте `web`; worker стартует после него.

## Бэкап

```bash
docker compose exec -T db-processed pg_dump -U neiromaster -Fc neiromaster > processed.dump
docker compose exec -T db-raw pg_dump -U neiromaster_raw -Fc neiromaster_raw > raw.dump
docker compose cp web:/app/data/secrets/pii.key ./pii.key   # хранить ОТДЕЛЬНО от дампов
```

Без `pii.key` зашифрованные ФИО, контакты и штатки не восстановить.

## Закрытый контур (без интернета)

На машине с интернетом собрать образы и упаковать их в архив:

```bash
docker compose build
docker save neiromaster-web neiromaster-worker postgres:16-alpine redis:7-alpine caddy:2-alpine \
  | gzip > neiromaster-images.tar.gz
```

В закрытом контуре — репозиторий (или только `docker-compose.yml` и `deploy/`) и архив:

```bash
docker load < neiromaster-images.tar.gz
docker compose up -d
```

Образы уже есть локально — compose ничего не скачивает и не собирает.

## Переезд с установки через install.sh

1. Дамп старой БД → в `db-processed`:
   `docker compose exec -T db-processed pg_restore -U neiromaster -d neiromaster --clean < old.dump`
2. Ключ ПДн → в том секретов: `docker compose cp data/secrets/pii.key web:/app/data/secrets/pii.key`
3. Оригиналы документов → в `db-raw`: скопировать `data/documents/` в контейнер и выполнить
   `docker compose exec web python backend/rawdb.py --backfill` (идемпотентно; берёт и из S3, если он настроен).
