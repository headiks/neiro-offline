"""
Единый клиент онлайн-модели DeepSeek (OpenAI-совместимый /chat/completions).

Заменяет прежний оффлайн-LLM (Ollama qwen). Один источник вызова для rag.py и
docpipe/llm.py: детерминированный вывод (temperature=0), строгий JSON-режим
(response_format), повтор при сетевых сбоях и таймауте — то, что нужно для
скорости и надёжности запросов к облачной модели.

Ключ/модель/URL — из окружения (или из config, который грузит .env):
  DEEPSEEK_API_KEY   — ключ (обязателен)
  DEEPSEEK_MODEL     — модель для генерации/разметки (по умолчанию deepseek-chat)
  DEEPSEEK_BASE_URL  — базовый URL (по умолчанию https://api.deepseek.com)

Почему deepseek-chat, а не deepseek-reasoner: reasoner тратит бюджет вывода на
цепочку рассуждений и не отдаёт объёмный структурированный JSON (пустой/обрезанный
ответ), а json-режим не поддерживает. Для нашей разметки/генерации нужен именно
компактный строгий JSON — его даёт deepseek-chat.
"""
import os
import time

import requests

import pii
from ratelimit import deepseek_slot

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

# Таймауты (сек). Генерация/разметка длинных ответов бывает медленной под нагрузкой.
TIMEOUT = int(os.environ.get("DEEPSEEK_TIMEOUT", "300"))
MAX_RETRIES = int(os.environ.get("DEEPSEEK_MAX_RETRIES", "3"))
_RETRYABLE = (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
              requests.exceptions.SSLError)


def chat(system: str, user: str, *, json_mode: bool = False, model: str = None,
         temperature: float = 0.0, max_tokens: int = 8192, timeout: int = None,
         retries: int = None) -> str:
    """Один запрос к DeepSeek, возвращает message.content.

    json_mode=True — response_format=json_object (строгий JSON без ```-заборов).
    Слово «JSON» должно присутствовать в промпте (требование API) — в наших
    системных промптах оно есть. При обрыве по длине или пустом ответе — ошибка.
    """
    # ВАЖНО: ключ/URL/модель читаем в момент ВЫЗОВА, а не импорта. Иначе, если модуль
    # импортируется до того, как config загрузит .env, ключ был бы пустым навсегда.
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or API_KEY
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or BASE_URL).rstrip("/")
    use_model = model or os.environ.get("DEEPSEEK_MODEL") or MODEL
    if not api_key or api_key.startswith("sk-клю") or api_key in ("sk-ключ", "sk-key"):
        raise RuntimeError("DEEPSEEK_API_KEY не задан (env или .env).")
    # ПДн не покидают сервер: в модель уходят метки [ФИО_1], [ТЕЛ_1]…, в ответе они
    # заменяются обратно. Системные промпты — наши, их не трогаем.
    masker = pii.Masker()
    user = masker.mask(user)
    body = {
        "model": use_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    # 429/5xx у DeepSeek транзиентны (их API часто отдаёт 503 «Service is too busy»)
    # — повторяем с нарастающей паузой, как и сетевые сбои.
    RETRY_STATUS = {429, 500, 502, 503, 504}
    # retries — переопределение числа попыток на вызов: некритичным путям (классификация
    # с векторным фолбэком) хватает 1, чтобы быстро деградировать при недоступности DeepSeek,
    # а не ждать полный бюджет ретраев. Генерация оставляет дефолт (устойчивость).
    max_att = max(1, retries) if retries else MAX_RETRIES
    r = None
    for attempt in range(1, max_att + 1):
        try:
            # Глобальный лимитер: не больше DEEPSEEK_MAX_CONCURRENCY вызовов в полёте
            # на весь кластер — защита от шторма 503 под высокой нагрузкой.
            with deepseek_slot():
                r = requests.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json=body, timeout=timeout or TIMEOUT,
                )
        except _RETRYABLE as e:
            if attempt == max_att:
                raise RuntimeError(f"DeepSeek: сеть недоступна после {max_att} попыток: {e}")
            time.sleep(2 * attempt)
            continue
        if r.status_code in RETRY_STATUS and attempt < max_att:
            time.sleep(2 * attempt)          # перегрузка/лимит — ждём и повторяем
            continue
        break
    if r.status_code >= 400:
        hint = " (DeepSeek перегружен — повторите позже)" if r.status_code in RETRY_STATUS else ""
        raise RuntimeError(f"DeepSeek API {r.status_code}{hint}: {r.text[:300]}")
    data = r.json()
    _record_usage(use_model, data.get("usage") or {})
    ch = data["choices"][0]
    content = (ch.get("message") or {}).get("content") or ""
    if ch.get("finish_reason") == "length":
        raise RuntimeError("DeepSeek: ответ обрезан по лимиту (увеличь max_tokens "
                           "или уменьши входной фрагмент).")
    if not content.strip():
        raise RuntimeError("DeepSeek: пустой ответ модели.")
    # Ответ в JSON: значения возвращаем JSON-экранированными (кавычки в названиях и т.п.).
    return masker.unmask(content, json_safe=json_mode or pii.looks_like_json(content))


def _record_usage(model: str, usage: dict):
    """Учёт токенов по дням (Redis-хэш nm:llm:YYYY-MM-DD, живёт 90 дней) — чтобы расход
    было видно без личного кабинета DeepSeek (GET /api/llm-usage). Сбой учёта не мешает."""
    try:
        from redis_conn import get_redis
        rd = get_redis()
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        print(f"[deepseek] {model}: prompt={pt} completion={ct}")
        if rd is None:
            return
        key = f"nm:llm:{time.strftime('%Y-%m-%d')}"
        pipe = rd.pipeline()
        pipe.hincrby(key, "calls", 1)
        pipe.hincrby(key, "prompt_tokens", pt)
        pipe.hincrby(key, "completion_tokens", ct)
        pipe.hincrby(key, "cache_hit_tokens", int(usage.get("prompt_cache_hit_tokens") or 0))
        pipe.expire(key, 90 * 24 * 3600)
        pipe.execute()
    except Exception:
        pass


def usage_by_day(days: int = 14) -> list:
    """Расход токенов за последние days дней (новые сверху). Без Redis — пусто."""
    from datetime import date, timedelta
    from redis_conn import get_redis
    rd = get_redis()
    if rd is None:
        return []
    out = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        h = rd.hgetall(f"nm:llm:{d}") or {}
        if h:
            out.append({"date": d, **{k: int(v) for k, v in h.items()}})
    return out
