// Статусы -> тон бейджа и подпись. Тоны — из дизайн-токенов (tokens.json → status):
// ok (зелёный), warn (янтарный), danger (красный), accent (акцент), muted (нейтральный).
export type Tone = "ok" | "warn" | "danger" | "accent" | "muted";
export type StatusView = { tone: Tone; label: string };

export const QUESTION_STATUS: Record<string, StatusView> = {
  open: { tone: "muted", label: "Ждёт ответа" },
  resolved: { tone: "ok", label: "Отвечено" },
  sos: { tone: "danger", label: "ЧС" },
};

/** Кому адресован вопрос, написанный кнопкой у сообщения (questions.ADDRESSEES). */
export const ADDRESSEE: Record<string, { to: string; title: string }> = {
  mentor: { to: "Наставнику", title: "Вопрос наставнику" },
  manager: { to: "Руководителю", title: "Вопрос руководителю" },
  hr: { to: "В отдел кадров", title: "Вопрос в отдел кадров" },
};

export const ROLE: Record<string, StatusView> = {
  owner: { tone: "accent", label: "Суперадмин" },
  admin: { tone: "ok", label: "Администратор" },
  employee: { tone: "muted", label: "Сотрудник" },
};

/** Статус адаптации сотрудника (users.adaptation_status + наличие плана). */
export function employeeStatus(status?: string | null, hasPlan = true, active = true): StatusView {
  if (!active) return { tone: "muted", label: "Заблокирован" };
  if (status === "paused") return { tone: "warn", label: "На больничном" };
  if (!hasPlan) return { tone: "danger", label: "Нет плана" };
  if (status === "active") return { tone: "ok", label: "Проходит адаптацию" };
  if (status === "done") return { tone: "muted", label: "Завершил" };
  return { tone: "accent", label: "Ждёт выхода" };
}

/** Документ в базе знаний: uploaded -> processing/reanalyzing -> indexed | error; confidential — хранится, ИИ не читает. */
export function docStatus(status?: string | null): StatusView {
  switch (status) {
    case "indexed": return { tone: "ok", label: "Готов" };
    case "error": return { tone: "danger", label: "Ошибка" };
    case "uploaded": return { tone: "warn", label: "В очереди" };
    case "reanalyzing": return { tone: "warn", label: "Переразметка" };
    case "confidential": return { tone: "muted", label: "Не отправляется в ИИ" };
    default: return { tone: "warn", label: "Обработка" };
  }
}

/** Текст сообщения плана (planner: generated | error | skipped | pending). */
export function textStatus(status?: string | null, hasText = false): StatusView {
  if (status === "error") return { tone: "danger", label: "Ошибка" };
  if (status === "skipped") return { tone: "warn", label: "Пропущено" };
  if (hasText) return { tone: "ok", label: "Готов" };
  return { tone: "danger", label: "Нет текста" };
}

export const COVERAGE = {
  found: { tone: "ok", label: "Документ найден" } as StatusView,
  missing: { tone: "warn", label: "Нет документа" } as StatusView,
};
