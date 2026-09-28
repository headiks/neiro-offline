"""База знаний: загрузка и разбор документов, смысловые папки, хранилище оригиналов."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

import config
import storage
import indexing
import documents
import folders
import users
import activitylog
from config import MAX_UPLOAD_BYTES
from deps import _bg, require_admin, admin_only, owner_only, globaltest_only, can_see_doc, visible_documents, ensure_doc_access

router = APIRouter()


def _require_known(filename: str) -> dict:
    """Документ из реестра или 404. Имя из URL идёт в пути к файлам — принимаем только то,
    что система сама сохранила (без «..» и прочих обходов каталога)."""
    if filename in (".", "..") or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Документ не найден")
    doc = next((d for d in indexing.list_documents() if d.get("filename") == filename), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return doc


# ---------- Управление документами ----------
@router.get("/documents")
def get_documents(user: dict = Depends(require_admin)):
    """Список документов в базе с их статусом индексации (загружен / обрабатывается / готов / ошибка).
    Суперадмину — все документы, администратору — только его собственные загрузки."""
    return {"documents": visible_documents(user),
            "scope": "all" if users.is_owner(user) else "own"}


@router.get("/documents/board")
def get_documents_board(plan_id: str | None = None, user: dict = Depends(require_admin)):
    """Данные экрана «этапы ↔ документы»: по каталогу этапов или (plan_id) по этапам и
    подэтапам конкретного плана — видно, каким подэтапам плана не хватает документов.
    Привязка — по LLM-разметке docpipe (не по косинусу), score = уверенность модели.
    Администратор видит на доске только свои документы."""
    import docpipe
    allowed = {d["filename"] for d in visible_documents(user)}
    plan_stages, docs = docpipe.document_assignments(filenames=allowed)
    if plan_id:
        import planner
        plan = planner.load_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="План не найден")
        plan_stages, docs = planner.plan_board(plan, docs)
    return documents.build_board(plan_stages, docs)


@router.get("/documents/table")
def get_documents_table(user: dict = Depends(require_admin)):
    """Табличные данные по документам: строки — из реестра (то, что видит этот
    администратор), привязка к подэтапам — по LLM-разметке docpipe, как и на доске."""
    import docpipe
    visible = visible_documents(user)
    _, assigned = docpipe.document_assignments(filenames={d["filename"] for d in visible})
    subs_by_doc = {d["filename"]: d["substages"] for d in assigned}
    rows = []
    for d in visible:
        subs = subs_by_doc.get(d["filename"], [])
        rows.append({
            "filename": d["filename"], "sha256": d.get("sha256") or "",
            "mime": Path(d["filename"]).suffix.lstrip(".").lower(),
            "size_bytes": d.get("size_bytes"), "status": d.get("status"),
            "uploaded_at": d.get("uploaded_at"), "uploaded_by": d.get("uploaded_by_name") or "",
            "summary": d.get("summary") or "", "keywords": [], "embedding_dim": None,
            "folders": d.get("folders") or [], "substages": subs,
            "stage_ids": sorted({s["substage_id"].split(".")[0] for s in subs if s.get("substage_id")}),
        })
    return {"documents": rows}


@router.get("/documents/{filename}/substage-map")
def get_document_substage_map(filename: str, user: dict = Depends(require_admin)):
    """Разбивка документа по подэтапам — по LLM-разметке (блоки, обоснование why,
    «общая информация»). Раньше здесь была приблизительная косинусная оценка; теперь
    это то же, что показывает «Разбор документа»."""
    ensure_doc_access(user, filename)
    import docpipe
    data = docpipe.document_breakdown(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Документ ещё не размечен LLM")
    return data


@router.post("/documents/{filename}/reindex")
def reindex_document(filename: str, user: dict = Depends(require_admin)):
    """То же, что /reanalyze (этот адрес зовёт служебная таблица документов): переразметка
    в worker'е. Раньше шла синхронно в web — docling и DeepSeek держали поток web-процесса,
    а в Docker образ web собран без docling."""
    return reanalyze_one(filename, user)


@router.post("/documents/upload")
def upload_document(file: UploadFile = File(...), mode: str = "", confidential: bool = False,
                    user: dict = Depends(require_admin)):
    """
    Загрузка нового регламента. Файл сохраняется в data/documents/ (+ S3) и ставится
    в фоновую очередь: docling-разбор -> разметка секций DeepSeek по этапам/подэтапам.
    Ответ приходит сразу (202) с идентификатором задачи; прогресс —
    через GET /documents/jobs/{job_id}. Тяжёлый разбор PDF не держит запрос.
    mode — что делать, если документ с таким именем уже есть: replace / separate.
    """
    if mode not in ("", "replace", "separate"):
        raise HTTPException(status_code=400, detail="mode: replace или separate")
    import deepseek
    deepseek.require_online("Загрузка документов")   # до сохранения: файл без разбора не нужен
    # Читаем не больше лимита +1 байт: иначе гигабайтный файл целиком буферизуется в
    # RAM ещё до проверки размера (потенциальный OOM). Лишний байт нужен, чтобы отличить
    # «ровно лимит» от «больше лимита».
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")

    # Дедупликация по содержимому (SHA-256): тот же файл не грузим и не индексируем заново.
    existing = indexing.find_duplicate(content)
    if existing:
        # Тот же файл уже обработан (кем угодно, в т.ч. другим админом) — повторно не
        # разбираем и не платим за обработку.
        return JSONResponse(status_code=200, content={
            "duplicate": True, "filename": existing["filename"],
            "uploaded_at": existing.get("uploaded_at"),
            "message": f"Такой файл уже загружен ранее ({existing['filename']}) — повторная обработка не требуется",
        })

    # Новая версия: файл с тем же именем, но другим содержимым. Раньше молча перезаписывался;
    # теперь спрашиваем: mode=replace — заменить старую версию, mode=separate — сохранить рядом.
    name = indexing.safe_filename(file.filename)
    old = next((d for d in indexing.list_documents() if d.get("filename") == name), None)
    if old and mode not in ("replace", "separate"):
        return JSONResponse(status_code=409, content={
            "conflict": "same_name", "filename": name, "uploaded_at": old.get("uploaded_at"),
            "uploaded_by_name": old.get("uploaded_by_name"),
            "can_replace": users.can_edit_doc(user, old),
            "message": f"Документ «{name}» уже есть. Заменить старую версию или сохранить как отдельный?",
        })
    if old and mode == "replace":
        ensure_doc_access(user, name, write=True)
        indexing.delete_document(name)
        try:
            documents.remove_by_filename(name)
        except Exception:
            pass
    upload_name = indexing.free_filename(name) if old and mode == "separate" else file.filename

    try:
        # uploader -> владелец документа: задаёт путь <суперадмин>/<админ>/<файл>
        # в S3 и определяет, кому документ будет виден.
        filepath = indexing.save_uploaded_file(upload_name, content, uploader=user,
                                               confidential=confidential)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if confidential:
        activitylog.log("action", user=user, path="/documents/upload",
                        detail={"action": "document_upload_confidential", "filename": filepath.name})
        return JSONResponse(status_code=201, content={
            "filename": filepath.name, "status": "confidential",
            "message": "Документ сохранён и в ИИ не отправляется"})

    # Индексация = docling-разбор + классификация/разметка docpipe (этапы/подэтапы,
    # метки в Postgres). Векторов/эмбеддингов нет. Разметку делает сам index_document
    # (через docpipe.ingest), отдельная постановка в очередь docpipe больше не нужна.
    job = indexing.enqueue_document(filepath)
    activitylog.log("action", user=user, path="/documents/upload",
                    detail={"action": "document_upload", "filename": filepath.name})
    return JSONResponse(status_code=202, content=job)


@router.get("/api/s3/list", dependencies=globaltest_only)   # обозреватель S3 — за /globaltest
def api_s3_list(prefix: str = "", recursive: bool = False,
                user: dict = Depends(require_admin)):
    """Листинг бакета (метаданные): «папки» + файлы уровня, либо рекурсивно. Хранилище
    может быть на другом сервере — endpoint/bucket отдаём в ответе.

    Суперадмин ходит по всему бакету (его папка — корень структуры), обычный
    администратор заперт в СВОЁМ подкаталоге <суперадмин>/<админ>/: запрошенный
    префикс вне него подменяется на собственный, чужие файлы не листаются."""
    import config
    import storage
    home = ""
    if not users.is_owner(user):
        top, own = indexing.owner_dirs(user)
        home = f"{config.S3_PREFIX}{top}/{own}/"
        if not (prefix or "").startswith(home):
            prefix = home
    data = storage.list_objects(prefix=prefix, delimiter=("" if recursive else "/"))
    data["home"] = home          # ниже этого префикса администратору спускаться нельзя
    data["scope"] = "all" if users.is_owner(user) else "own"
    return data


@router.get("/documents/jobs/{job_id}", dependencies=admin_only)
def get_document_job(job_id: str):
    """Статус фоновой индексации загруженного документа."""
    job = indexing.get_index_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@router.get("/documents/label-jobs/{job_id}", dependencies=admin_only)
def get_label_job(job_id: str):
    """Статус фоновой LLM-разметки docpipe (проход по секциям, возобновляемый)."""
    import docpipe
    job = docpipe.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача разметки не найдена")
    return dict(job)


@router.get("/documents/labeled")
def list_labeled_documents(user: dict = Depends(require_admin)):
    """Имена документов, размеченных docpipe (для просмотрщика разбора). Только свои."""
    import docpipe.store as dstore
    allowed = {d.get("filename") for d in visible_documents(user)}
    return {"documents": [f for f in dstore.list_documents() if f in allowed]}


@router.get("/documents/{filename}/labels")
def get_document_labels(filename: str, user: dict = Depends(require_admin)):
    """Полный разбор документа (docpipe): карточка документа + блоки (секции) с текстом,
    метками (этапы/подэтапы/профессии), обоснованием «почему», названиями и описаниями
    этапов/подэтапов, и чанками каждого блока. Источник правды по разметке (LLM, temp=0)."""
    ensure_doc_access(user, filename)
    import docpipe
    data = docpipe.document_breakdown(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Документ не размечен docpipe (загрузите заново или дождитесь разметки)")
    return data


def _delete_one(user: dict, filename: str):
    """Удаляет документ: разметку docpipe (PostgreSQL), оригинал (диск + S3), кэш docling.
    HTTPException — нет прав (403) или документа (404)."""
    ensure_doc_access(user, filename, write=True)
    if not indexing.delete_document(filename):
        raise HTTPException(status_code=404, detail="Документ не найден")
    try:
        documents.remove_by_filename(filename)
    except Exception:
        pass


@router.delete("/documents/{filename}")
def remove_document(filename: str, user: dict = Depends(require_admin)):
    _delete_one(user, filename)
    _bg(indexing.docs_changed)   # тексты, опиравшиеся на документ, обновятся (только они)
    activitylog.log("action", user=user, path=f"/documents/{filename}",
                    detail={"action": "document_delete", "filename": filename})
    return {"filename": filename, "deleted": True}


class FilenamesRequest(BaseModel):
    filenames: list[str] = Field(default_factory=list, max_length=1000)


@router.post("/documents/bulk-delete")
def bulk_delete_documents(req: FilenamesRequest, user: dict = Depends(require_admin)):
    """Удаление отмеченных документов. Права — как у одиночного удаления; чужие и уже
    удалённые — в skipped с причиной. Пересчёт сообщений — один раз на всю пачку."""
    deleted, skipped = [], []
    for name in dict.fromkeys(req.filenames):
        try:
            _delete_one(user, name)
            deleted.append(name)
        except HTTPException as e:
            skipped.append({"filename": name, "reason": e.detail})
    if deleted:
        _bg(indexing.docs_changed)
    activitylog.log("action", user=user, path="/documents/bulk-delete",
                    detail={"action": "documents_bulk_delete", "deleted": deleted})
    return {"deleted": len(deleted), "skipped": skipped}


# ---------- Смысловые папки (логические категории; ими управляет человек) ----------
class FolderRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    criteria: list | None = None
    stage_ids: list | None = None
    enabled: bool | None = None


@router.get("/folders", dependencies=admin_only)
def get_folders():
    """Смысловые папки базы знаний. Их создаёт и редактирует человек — ИИ только
    классифицирует документы внутрь существующих папок, но не заводит новые."""
    result = folders.list_folders()
    try:
        counts = indexing.folder_doc_counts()
    except Exception:
        counts = {}   # счётчик документов не должен ронять список папок
    for f in result:
        f["documents"] = counts.get(f["slug"], 0)
    return {"folders": result}




@router.post("/folders", dependencies=admin_only)
def create_folder(req: FolderRequest):
    try:
        folder = folders.create_folder(req.name, req.description or "", req.criteria, req.stage_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return folder


@router.put("/folders/{folder_id}", dependencies=admin_only)
def update_folder(folder_id: str, req: FolderRequest):
    existing = folders.get_folder(folder_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    try:
        folder = folders.update_folder(folder_id, **req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return folder


@router.delete("/folders/{folder_id}", dependencies=admin_only)
def delete_folder(folder_id: str):
    """Удаляет только логическую категорию — документы остаются в общей базе знаний."""
    folder = folders.get_folder(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    folders.delete_folder(folder_id)
    return {"deleted": True}


# ---------- Повторный анализ и уточнения по документам (ТЗ §8, §16, §26) ----------
class ClarifyRequest(BaseModel):
    clarification: str = Field(max_length=4000)


@router.post("/documents/reanalyze", dependencies=owner_only)
def reanalyze_documents():
    """Полный повторный анализ ВСЕЙ базы под текущую структуру папок (фоново).
    Только суперадмин: операция задевает документы всех администраторов.
    Прогресс пакета — GET /documents/jobs/{job_id} (документ i из N)."""
    import uuid
    import jobs
    job_id = f"reanalyze-{uuid.uuid4().hex[:8]}"
    indexing._set_index_job(job_id, status="queued", done=0, total=0)
    jobs.enqueue_reanalyze_all(job_id)
    return {"started": True, "job_id": job_id}


@router.post("/documents/{filename}/reanalyze")
def reanalyze_one(filename: str, user: dict = Depends(require_admin)):
    """Переанализ одного документа — фоново; статус (reanalyzing -> indexed/error)
    виден в списке документов рядом с этим документом."""
    _require_known(filename)
    ensure_doc_access(user, filename, write=True)
    import jobs
    jobs.enqueue_reanalyze_document(filename)
    return {"started": True}


@router.post("/documents/{filename}/reprocess")
def reprocess_one(filename: str, user: dict = Depends(require_admin)):
    """Полный повторный разбор документа с нуля (docling -> разметка docpipe) — для файлов
    со статусом error/uploaded, которым обычный переанализ не помогает. Ставит файл в
    фоновую очередь индексации."""
    _require_known(filename)
    ensure_doc_access(user, filename, write=True)
    if indexing.is_confidential(filename):
        raise HTTPException(status_code=400, detail="Документ помечен «не отправлять в ИИ»")
    fp = indexing.DOCS_DIR / filename
    if not fp.exists():
        raise HTTPException(status_code=404, detail="Файл-оригинал не найден в хранилище")
    job = indexing.enqueue_document(fp)
    return {"status": "queued", "job": job}


class ConfidentialRequest(BaseModel):
    confidential: bool


@router.post("/documents/{filename}/confidential")
def set_document_confidential(filename: str, req: ConfidentialRequest, user: dict = Depends(require_admin)):
    """Чувствительный документ (положение об оплате труда и т.п.): «не отправлять в ИИ».
    Включение удаляет разметку — фрагменты документа больше не уходят в модель и не попадают
    в сообщения и ответы; сообщения, которые на него опирались, обновятся без него.
    Выключение отправляет документ на обычную обработку."""
    _require_known(filename)
    ensure_doc_access(user, filename, write=True)
    entry = indexing.set_confidential(filename, req.confidential)
    if req.confidential:
        _bg(indexing.docs_changed)
    activitylog.log("action", user=user, path=f"/documents/{filename}/confidential",
                    detail={"action": "document_confidential", "filename": filename, "on": req.confidential})
    return entry


@router.post("/documents/{filename}/clarify")
def clarify_document(filename: str, req: ClarifyRequest,
                     user: dict = Depends(require_admin)):
    """Текстовое уточнение пользователя (актуальность/архив/область действия — ТЗ §17).
    Исходный документ не переписывается — уточнение хранится как доп. контекст."""
    ensure_doc_access(user, filename, write=True)
    if not indexing.set_clarification(filename, req.clarification):
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"filename": filename, "clarification": req.clarification}
