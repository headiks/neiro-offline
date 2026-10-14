// Общие типы сайта (frontend/) и приложения (mobile/): одни и те же ответы API.

export type Role = "owner" | "admin" | "employee";

export type Me = {
  id: string;
  username: string | null;
  full_name: string;
  role: Role;
  active: boolean;
  status: string;                 // planned | active | paused | done | …
  position?: string;
  department?: string;
  mentor?: string;
  manager?: string;
  phone?: string;
  email?: string;
  plan_id?: string | null;
  start_date?: string | null;
  must_change_credentials?: boolean;
};

// ---- Сообщения плана (инбокс) ----
export type ChecklistItem = { id: string; text: string };
/** Кому сотрудник может написать из сообщения: наставнику, руководителю, в отдел кадров. */
export type ContactRole = "mentor" | "manager" | "hr";
export type Contact = { role: ContactRole; label: string };
// contact — вариант опроса «Написать наставнику» заодно открывает письмо этой роли.
export type QuizOption = { id: string; text: string; correct?: boolean; contact?: ContactRole };
// type: single | multi (несколько вариантов, ответ — id через запятую) | open (свой текст)
export type QuizQuestion = { id: string; text: string; type?: string; options?: QuizOption[]; explanation?: string };

export type Payload =
  | { type: "checklist"; intro?: string; items: ChecklistItem[]; contacts?: Contact[] }
  | { type: "survey" | "quiz"; intro?: string; questions: QuizQuestion[]; contacts?: Contact[] }
  | { type?: string; intro?: string; contacts?: Contact[]; [k: string]: unknown };

export type Answers = Record<string, string | boolean>;

export type Msg = {
  id: string;                     // <сотрудник>:<сообщение> — для «прочитано» и ответов
  message_id?: string;
  kind?: string;                  // message | reminder | checklist | survey | quiz | handover | …
  payload?: Payload | null;
  answers?: Answers | null;
  title?: string;                 // «Этап — Подэтап»
  body?: string;
  send_at?: string;
  delivered_at?: string | null;
  read_at?: string | null;
  status?: string;                // delivered | read
  seq?: number | null;            // позиция в плане: порядок сообщений одного времени
};

export type Inbox = { messages: Msg[]; unread: number };

// ---- Вопросы специалисту ----
export type QuestionStatus = "open" | "resolved";

export type MyQuestion = {
  id: string;
  created_at: string;
  question: string;
  status: QuestionStatus;
  answer?: string | null;
  answered_at?: string | null;
  reason?: string;
  risk_type?: string | null;
  addressee?: ContactRole | null;   // вопрос, написанный кнопкой «Написать наставнику» и т.п.
};

// ---- Свой план адаптации ----
export type ScheduleItem = {
  message_id: string;
  stage: { id: string; title: string; order: number };
  substage: { id: string; title: string; order: number; kind?: string; brief?: string };
  // send_at — с учётом больничных; planned_at — исходное время по плану (если сдвинуто).
  schedule: { stage_day?: number; time?: string; offset_days: number; send_at: string | null; planned_at?: string };
  content?: { text?: string };
  status?: string;
};

export type MySchedule = {
  plan_id: string;
  plan_title?: string;
  start_date: string;
  timezone?: string;
  plan_generated?: boolean;
  paused?: boolean;                               // сейчас на больничном: план стоит
  pauses?: { start: string; end: string | null }[]; // больничные (UTC), у идущего end = null
  employee?: { status?: string; mentor?: string; manager?: string };
  messages: ScheduleItem[];
};

// ---- Ассистент ----
export type AskResult = {
  question: string;
  session_id: string;
  route: string;                  // rag | escalate | error | …
  answer: string | null;
  escalated?: boolean;
  sources?: string[];
  error?: string | null;
};

export type LoginResult = {
  token?: string;
  username: string;
  role: Role;
  must_change_credentials: boolean;
};
