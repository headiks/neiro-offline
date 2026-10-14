// Типы ответов админских API (кабинетные — в shared/types.ts).
import type { Role } from '@shared/types';

export type AdminUser = {
  id: string;
  username: string | null;
  full_name: string;
  role: Role;
  active: boolean;
  status: string;
  position?: string;
  department?: string;
  phone?: string;
  email?: string;
  mentor?: string;
  manager?: string;
  plan_id?: string | null;
  plan_title?: string | null;
  plan_generated?: boolean;
  start_date?: string | null;
  notes?: string;
  temp_password?: string;
  must_change_credentials?: boolean;
  created_at?: string;
};

export type PlanSummary = { plan_id: string; title: string; role?: string; generated?: boolean; updated_at?: string };

export type Duration = { value: number; unit: 'hours' | 'days' | 'weeks' | 'months' };

export type Substage = {
  id?: string | null;
  uid?: string;
  catalog_id: string | null;
  title: string;
  kind: string;
  brief: string;
  source?: string;
  tags?: string[];
  topic_keys?: string[];
  schedule: { day: number; time: string };
};

export type Stage = {
  id?: string | null;
  uid?: string;
  catalog_id: string | null;
  title: string;
  description?: string;
  anchor: 'from_start' | 'before_start';
  duration: Duration;
  substages: Substage[];
};

export type Plan = {
  plan_id: string | null;
  title: string;
  role?: string;
  group_daily?: boolean;
  updated_at?: string;
  stages: Stage[];
  __demo?: boolean;
};

export type Catalog = {
  stages: {
    id: string; title: string; description: string; anchor: 'from_start' | 'before_start'; default_duration: Duration;
    substage_templates: { id: string; title: string; kind: string; brief: string; tags?: string[]; default_time?: string }[];
  }[];
  substage_kinds: { id: string; title: string }[];
};

export type Doc = {
  filename: string;
  status: string;
  mime?: string;
  size_bytes?: number;
  chunks?: number;
  uploaded_at?: string;
  uploaded_by?: string;
  uploaded_by_name?: string;
  department?: string;
  storage_path?: string;
  folders?: string[];
  summary?: string;
  clarification?: string;
  error?: string;
  progress?: number;
  phase?: string;
  confidential?: boolean;
};

export type Coverage = { plan_id: string; title?: string; covered: number; total: number };

export type BoardDoc = { filename: string; score?: number | null; mime?: string };
export type Board = {
  stages: { id?: string; title: string; description?: string; documents?: BoardDoc[];
            substages?: { id?: string; title: string; documents?: BoardDoc[] }[] }[];
  unassigned?: BoardDoc[];
  stats?: { stages?: number; substages?: number; documents?: number; unassigned?: number };
};

export type AdminQuestion = {
  id: string;
  created_at: string;
  user_id?: string;
  user_name?: string;
  position?: string;
  department?: string;
  contact?: string;
  mentor?: string;
  question: string;
  resolved_question?: string | null;
  reason?: string;
  risk_type?: string | null;
  addressee?: string | null;      // написан кнопкой «Написать наставнику» и т.п. (mentor | manager | hr)
  status: 'open' | 'resolved';
  answer?: string | null;
  answered_by?: string | null;
  answered_at?: string | null;
};

export type ScheduleMessage = {
  message_id: string;
  stage: { id: string; title: string; order: number };
  substage: { id: string; title: string; order: number; kind?: string; brief?: string };
  schedule: { anchor?: string; offset_days: number; time: string; send_at: string | null; planned_at?: string };
  content: { text?: string; hr_note?: string };
  status?: string;
  error?: string | null;
  sources?: ({ source?: string; filename?: string; title?: string } | string)[];
  folders_used?: string[];
};

export type Job = {
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  done: number;
  total: number;
  current?: string;
  reused?: number;
  skipped?: number;
  errors?: number;
  llm_calls?: number;
  llm_planned?: number;
  error?: string;
};
