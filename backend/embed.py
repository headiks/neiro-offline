"""
embed.py — векторы текста для поиска по смыслу (база ответов qacache).

Модель — bge-m3 в локальном llama.cpp-сервере (служба embed в docker-compose.yml,
OpenAI-совместимый /v1/embeddings). Работает на CPU без интернета: около 0,2 с на вопрос.

    NEIROMASTER_EMBED_URL — адрес сервера (http://embed:8080). Пусто — поиск по смыслу
    выключен, база ответов ищет только точные совпадения (как раньше).

Векторы нормированы: косинус = скалярное произведение.
"""
import os

import numpy as np
import requests

BATCH = 16
TIMEOUT = 120


def url() -> str:
    return os.environ.get("NEIROMASTER_EMBED_URL", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(url())


def vectors(texts: list) -> "np.ndarray | None":
    """(len(texts), dim) float32, строки нормированы. None — сервер выключен или недоступен:
    вызывающий работает без поиска по смыслу, а не падает."""
    if not texts or not enabled():
        return None
    out = []
    try:
        for i in range(0, len(texts), BATCH):
            r = requests.post(f"{url()}/v1/embeddings", json={"input": texts[i:i + BATCH]},
                              timeout=TIMEOUT)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
    except Exception as e:
        print(f"[embed] сервер эмбеддингов недоступен: {e}")
        return None
    m = np.asarray(out, dtype=np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
    return m
