# Перенос НейроМастер на другой сервер

Пошаговый гайд: поднять проект на новом Linux-сервере, перенести данные,
восстановить доступы, переключить домен. Ничего секретного в репозитории нет —
все ключи и пароли живут в env-файлах и `secrets/` (в git не попадают).

---

## 0. Что это за система (архитектура)

| Компонент | Что | Где |
|-----------|-----|-----|
| Веб-приложение | FastAPI + gunicorn/uvicorn, порт **8000** | systemd `rag-app` |
| Воркеры | RQ, тяжёлые задачи (классификация/генерация) | systemd `rag-worker@1..4` |
| БД | **PostgreSQL 16** (аккаунты, планы, расписания, сообщения, push-токены) | Docker `neiromaster-pg`, том `pg_data`, `127.0.0.1:5432` |
| Очередь/стейт | **Redis 7** | Docker `neiromaster-redis`, `127.0.0.1:6379` |
| HTTPS/прокси | **Caddy** (авто-сертификат Let's Encrypt) | `neiromaster.duckdns.org → 127.0.0.1:8000` |
| LLM | Облачный **DeepSeek** (ключ в `.env`) | внешний API |
| Хранилище документов | **S3** (внешнее) | ключи в `.env.production` |
| Push | **FCM** (Firebase project `ru-neiromaster-app`) | `secrets/fcm-service-account.json` |
| Мобильное приложение | Expo/Android APK, package `ru.neiromaster.app` | у пользователей на телефонах |

Текущий сервер: Ubuntu 22.04, x86_64, Python 3.10, каталог `/root/neiromaster-ds`.

---

## 1. Доступы, которые надо подготовить заранее

Собери это ДО начала — иначе застрянешь на середине:

1. **Новый сервер** — Ubuntu 22.04+ x86_64, root или sudo, ≥ 4 ГБ RAM, ≥ 20 ГБ диск,
   открытые порты **80** и **443** наружу (для Caddy/HTTPS) и **22** (SSH).
2. **SSH-ключ** к новому серверу — сгенерируй и положи публичную часть на сервер:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/neiro_new -C neiro-deploy
   ssh-copy-id -i ~/.ssh/neiro_new.pub root@<NEW_IP>
   ```
3. **DeepSeek API-ключ** — тот же, что сейчас (лежит в `.env` на старом сервере,
   строка `DEEPSEEK_API_KEY`). Перенесём файлом, не пересоздавая.
4. **S3-доступ** — endpoint/bucket/ключи (в `.env.production`: `NEIROMASTER_S3_*`).
   Если оставляем **тот же bucket** — просто копируем эти строки, документы не трогаем.
5. **DuckDNS-токен** — с [duckdns.org](https://www.duckdns.org) (логин под своим аккаунтом,
   токен вверху). Нужен, чтобы переключить `neiromaster.duckdns.org` на новый IP.
6. **Firebase service-account** — уже есть файл `secrets/fcm-service-account.json`
   на старом сервере. Переносим как есть (Firebase-проект `ru-neiromaster-app` не меняется).
7. **Домен** — решить: оставляем `neiromaster.duckdns.org` (тогда мобильное приложение
   переустанавливать НЕ надо) или берём новый (тогда APK пересобрать, см. §7).

> Секреты передавай безопасно (scp, не мессенджеры). На новом сервере — `chmod 600`.

---

## 2. Снять данные со СТАРОГО сервера

```bash
OLD=root@161.104.44.239
KEY=~/.ssh/neiro_deploy      # ключ к старому серверу

# 2.1 Дамп PostgreSQL (схема + данные, сжато)
ssh -i $KEY $OLD 'docker exec neiromaster-pg pg_dump -U neiromaster -d neiromaster -Fc' > neiromaster.dump

# 2.2 Файловые данные приложения (очередь вопросов, планы-файлы, реестр, каталог, словари)
ssh -i $KEY $OLD 'tar czf - -C /root/neiromaster-ds data' > data.tgz

# 2.3 Секреты (env-файлы + FCM-ключ)
scp -i $KEY $OLD:/root/neiromaster-ds/.env               ./env.old
scp -i $KEY $OLD:/root/neiromaster-ds/.env.production    ./env.production.old
scp -i $KEY $OLD:/root/neiromaster-ds/secrets/fcm-service-account.json ./fcm-service-account.json
```

> **S3**: документы лежат в S3, в дампе/`data.tgz` их нет. Если оставляем тот же bucket —
> ничего копировать не нужно. Если переезжаем на новый bucket — скопируй объекты
> (`aws s3 sync s3://old s3://new` или `rclone`) и поправь `NEIROMASTER_S3_*` в env.

---

## 3. Поднять НОВЫЙ сервер

```bash
NEW=root@<NEW_IP>
KEY=~/.ssh/neiro_new

# 3.1 Код
ssh -i $KEY $NEW 'git clone https://github.com/headiks/neiromaster-deepseek /root/neiromaster-ds'

# 3.2 ВАЖНО: сначала кладём СТАРЫЕ секреты, чтобы install.sh создал контейнер Postgres
#     с тем же паролем, что в дампе (иначе DSN не сойдётся с БД).
scp -i $KEY ./env.old            $NEW:/root/neiromaster-ds/.env
scp -i $KEY ./env.production.old $NEW:/root/neiromaster-ds/.env.production
ssh -i $KEY $NEW 'mkdir -p /root/neiromaster-ds/secrets && chmod 700 /root/neiromaster-ds/secrets'
scp -i $KEY ./fcm-service-account.json $NEW:/root/neiromaster-ds/secrets/fcm-service-account.json
ssh -i $KEY $NEW 'chmod 600 /root/neiromaster-ds/.env /root/neiromaster-ds/.env.production /root/neiromaster-ds/secrets/fcm-service-account.json'

# 3.3 Установка: пакеты, venv, зависимости, Docker Postgres+Redis, systemd-юниты
#     install.sh увидит существующий .env.production и переиспользует его DSN/пароль.
ssh -i $KEY $NEW 'cd /root/neiromaster-ds && chmod +x install.sh && ./install.sh'

# 3.4 google-auth для FCM-пушей (в requirements он есть, но на всякий случай)
ssh -i $KEY $NEW 'cd /root/neiromaster-ds && .venv/bin/pip install -q "google-auth>=2.23.0"'
```

---

## 4. Восстановить данные на новом сервере

```bash
# 4.1 Файловые данные
scp -i $KEY ./data.tgz $NEW:/root/neiromaster-ds/
ssh -i $KEY $NEW 'cd /root/neiromaster-ds && tar xzf data.tgz && rm data.tgz'

# 4.2 Дамп Postgres -> в контейнер (перетирает пустую схему, созданную install.sh)
scp -i $KEY ./neiromaster.dump $NEW:/root/neiromaster-ds/
ssh -i $KEY $NEW 'docker exec -i neiromaster-pg pg_restore -U neiromaster -d neiromaster --clean --if-exists < /root/neiromaster-ds/neiromaster.dump; rm /root/neiromaster-ds/neiromaster.dump'
```

> Если `pg_restore` ругается на «database is being accessed» — сначала останови приложение
> (`systemctl stop rag-app 'rag-worker@*'`), восстанови, потом запусти (шаг 6).

---

## 5. Caddy (HTTPS) на новом сервере

```bash
# 5.1 Установка Caddy (официальный репозиторий)
ssh -i $KEY $NEW 'apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl && \
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && \
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list && \
  apt-get update && apt-get install -y caddy'
```

`/etc/caddy/Caddyfile`:
```
neiromaster.duckdns.org {
    reverse_proxy 127.0.0.1:8000
}
```
```bash
ssh -i $KEY $NEW 'systemctl restart caddy'
```
Caddy сам выпустит сертификат Let's Encrypt, как только DNS будет указывать на новый IP (§6).

---

## 6. Переключить домен и запустить

```bash
# 6.1 DuckDNS: указать домен на новый IP
curl "https://www.duckdns.org/update?domains=neiromaster&token=<DUCKDNS_TOKEN>&ip=<NEW_IP>"
#   ответ "OK" = успешно. Распространение — обычно минуты.

# 6.2 Запустить сервисы
ssh -i $KEY $NEW 'systemctl daemon-reload && systemctl enable --now rag-app && \
  for i in 1 2 3 4; do systemctl enable --now rag-worker@$i; done'
```

---

## 7. Мобильное приложение

- **Домен НЕ меняется** (`neiromaster.duckdns.org` переключён на новый IP) — **ничего не делать**,
  приложение продолжит работать, push тоже (Firebase-проект тот же).
- **Домен меняется** — правь `EXPO_PUBLIC_API_BASE`:
  - `mobile/src/config.ts` (дефолт) и `mobile/eas.json` (профили `preview`/`production`),
  - пересобери APK (см. `mobile/` — сборка через gradle или EAS),
  - раздай новый APK на телефоны. `google-services.json`/FCM менять НЕ нужно.

---

## 8. Проверка

```bash
NEW=root@<NEW_IP>; KEY=~/.ssh/neiro_new
ssh -i $KEY $NEW '
  echo "rag-app:  $(systemctl is-active rag-app)"
  for i in 1 2 3 4; do echo "worker@$i: $(systemctl is-active rag-worker@$i)"; done
  echo "pg:    $(docker exec neiromaster-pg pg_isready -U neiromaster)"
  echo "redis: $(docker exec neiromaster-redis redis-cli ping)"
  curl -s -o /dev/null -w "app HTTP %{http_code}\n" http://127.0.0.1:8000/
  .venv/bin/python -c "import push; t,p=push._access_token(); print(\"FCM OAuth OK:\", p)"
'
```
Затем в браузере: `https://neiromaster.duckdns.org` — открывается вход, замок HTTPS зелёный.
Проверь: логин админом, список сотрудников/планов на месте, задай тестовый вопрос →
ответь в админке → на телефоне придёт push.

---

## 9. Откат

DNS у DuckDNS переключается мгновенно — если что-то не так, верни старый IP:
```bash
curl "https://www.duckdns.org/update?domains=neiromaster&token=<DUCKDNS_TOKEN>&ip=<OLD_IP>"
```
Старый сервер не гаси, пока новый не проверен полностью. После успешной проверки —
останови сервисы на старом (`systemctl disable --now rag-app 'rag-worker@*'`).

---

## 10. Чеклист секретов (ничего не забыть)

- [ ] `.env` — `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL`
- [ ] `.env.production` — `NEIROMASTER_DB_DSN`, `REDIS_URL`, `NEIROMASTER_S3_*`, `WEB_CONCURRENCY`, `NEIROMASTER_DB_POOL`
- [ ] `secrets/fcm-service-account.json` (chmod 600)
- [ ] Дамп Postgres восстановлен (аккаунты, планы, расписания, push-токены)
- [ ] `data/` перенесён (очередь вопросов, реестр, каталог, словари имён)
- [ ] S3: тот же bucket ИЛИ скопирован новый + поправлены ключи
- [ ] DuckDNS указывает на новый IP, Caddy выдал сертификат
