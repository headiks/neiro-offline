// API сотрудника — один клиент для сайта и приложения. Отличается только транспорт:
// сайт ходит со своего origin по сессионной куке, приложение — на API_BASE с Bearer-токеном.
import type { AskResult, Inbox, LoginResult, Me, MyQuestion, MySchedule, Answers, ContactRole } from "./types";

export class ApiError extends Error {
  status: number;
  data: any;
  constructor(status: number, message: string, data?: any) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export type Transport = {
  base?: string;                                  // "" на сайте, https://… в приложении
  token?: () => Promise<string | null> | string | null;
  onUnauthorized?: () => void;                    // сессия истекла
  credentials?: "same-origin" | "include" | "omit";
};

function parse(text: string): any {
  try { return JSON.parse(text); } catch { return text ? { raw: text } : null; }
}

/** Текст ошибки FastAPI: detail строкой или списком ошибок валидации. */
export function errorText(data: any, status: number): string {
  const d = data && (data.detail ?? data.message);
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) return String(d[0]?.msg || "Проверьте введённые данные");
  if (status === 0) return "Нет связи с сервером";
  if (status === 429) return "Слишком много запросов. Подождите немного и повторите.";
  return `Ошибка ${status}`;
}

export function createClient(t: Transport) {
  async function request<T = any>(path: string, opts: RequestInit & { json?: unknown } = {}): Promise<T> {
    const headers: Record<string, string> = { ...(opts.headers as Record<string, string> | undefined) };
    let body = opts.body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    const token = t.token ? await t.token() : null;
    if (token) headers["Authorization"] = `Bearer ${token}`;
    let res: Response;
    try {
      res = await fetch(`${t.base || ""}${path}`, {
        ...opts, body, headers, credentials: t.credentials || "same-origin",
      });
    } catch {
      throw new ApiError(0, "Нет связи с сервером");
    }
    const data = parse(await res.text());
    if (!res.ok) {
      if (res.status === 401 && t.onUnauthorized && !path.startsWith("/api/login")) t.onUnauthorized();
      throw new ApiError(res.status, errorText(data, res.status), data);
    }
    return data as T;
  }

  const enc = encodeURIComponent;
  return {
    request,
    get: <T = any>(path: string) => request<T>(path),
    post: <T = any>(path: string, json?: unknown) => request<T>(path, { method: "POST", json: json ?? {} }),
    put: <T = any>(path: string, json?: unknown) => request<T>(path, { method: "PUT", json: json ?? {} }),
    del: <T = any>(path: string, json?: unknown) =>
      request<T>(path, json === undefined ? { method: "DELETE" } : { method: "DELETE", json }),

    // ---- Вход ----
    login: (username: string, password: string) =>
      request<LoginResult>("/api/login", { method: "POST", json: { username, password } }),
    logout: () => request("/api/logout", { method: "POST", json: {} }),
    me: () => request<Me>("/api/me"),

    // ---- Кабинет сотрудника ----
    mySchedule: () => request<MySchedule>("/api/my/schedule"),
    myMessages: () => request<Inbox>("/api/my/messages"),
    myQuestions: () => request<{ questions: MyQuestion[] }>("/api/my/questions"),
    askPerson: (to: ContactRole, text: string) =>
      request<{ question: MyQuestion }>("/api/my/questions", { method: "POST", json: { to, text } }),
    markRead: (id: string) => request(`/api/my/messages/${enc(id)}/read`, { method: "POST", json: {} }),
    answer: (id: string, answers: Answers) =>
      request(`/api/my/messages/${enc(id)}/answer`, { method: "POST", json: { answers } }),
    ask: (question: string, session_id?: string | null) =>
      request<AskResult>("/ask", { method: "POST", json: { question, session_id } }),
    setSick: (sick: boolean) =>
      request<{ status: string; sick: boolean }>("/api/my/status", { method: "POST", json: { sick } }),
    registerPushToken: (token: string, platform: string) =>
      request("/api/my/push-token", { method: "POST", json: { token, platform } }),
    removePushToken: (token: string) =>
      request("/api/my/push-token", { method: "DELETE", json: { token } }),
  };
}

export type Client = ReturnType<typeof createClient>;

/** Текст ответа ассистента для показа (эскалация, «не найдено», ошибка). */
export function answerText(res: AskResult): { text: string; escalated: boolean; error: boolean } {
  if (res.route === "error" || (res.error && !res.answer)) {
    return { text: res.error || "Не удалось обработать вопрос. Попробуйте ещё раз.", escalated: false, error: true };
  }
  const escalated = !!res.escalated || res.route === "escalate";
  const text = res.answer || (escalated
    ? "Вопрос передан специалисту — ответ появится в разделе «Мои вопросы»."
    : "Ответ не найден. Уточните вопрос или обратитесь к наставнику.");
  return { text, escalated, error: false };
}

/** Подсказки-вопросы под полем ассистента. */
export const SUGGESTIONS = ["Когда аванс?", "Где столовая?", "Что положено из спецодежды?", "Кто мой наставник?"];
