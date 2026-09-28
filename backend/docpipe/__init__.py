"""
docpipe — двухпроходная разметка внутренних документов относительно плана адаптации
(этапы → подэтапы) + наследование меток в мелкие чанки для RAG.

Слои: schema (DDL PG) · store (CRUD PG, источник правды) · core (чистая логика) ·
llm (DeepSeek, 2 прохода) · professions (матч со штаткой) · pipeline (оркестрация, очередь).

Публичный API ниже.
"""

from .schema import init_schema
from .store import (save_plan_version, get_plan_structure, get_job,
                    delete_by_filename, prune_orphans)
from .pipeline import (ingest, reindex, relabel_candidates, retrieve, blocks_for_substage,
                       chunks_for_substage, enqueue, requeue_stranded, document_breakdown,
                       document_assignments)

__all__ = [
    "init_schema", "sync_plan_from_catalog", "save_plan_version", "get_plan_structure",
    "get_job", "ingest", "reindex", "relabel_candidates", "retrieve",
    "blocks_for_substage", "chunks_for_substage",
    "enqueue", "requeue_stranded", "document_breakdown",
    "document_assignments", "delete_by_filename", "prune_orphans",
]


def sync_plan_from_catalog(version_id: str = "current") -> dict:
    """Заводит версию плана из каталога этапов (planner). Каталожные подэтапы -> подэтапы
    с описанием (brief). Относительно этой структуры идёт разметка (проход 2)."""
    import planner
    cat = planner.load_catalog()
    structure = {"stages": [{
        "id": st["id"], "title": st.get("title", ""), "description": st.get("description", ""),
        "substages": [{"id": f"{st['id']}.{sub['id']}", "title": sub.get("title", ""),
                       "description": sub.get("brief", "")}
                      for sub in st.get("substage_templates") or []],
    } for st in cat.get("stages") or []]}
    save_plan_version(structure, version_id)
    return structure
