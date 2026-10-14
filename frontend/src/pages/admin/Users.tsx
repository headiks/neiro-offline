// Пользователи: штатное расписание (ИИ разбирает файл → таблица на проверку → создание с
// выгрузкой логинов/паролей), список с фильтром по подразделению и поиском, карточка
// сотрудника, выдача доступа, пауза (больничный), блокировка, удаление, расписание.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Ban, CalendarDays, Check, Clock, Download, FileSpreadsheet, KeyRound, MoreHorizontal, Pause, Pencil, Play,
  Search, Trash2, TriangleAlert, UserPlus, UserX, Users as UsersIcon, X,
} from 'lucide-react';
import { employeeStatus, ROLE } from '@shared/status';
import { ruDate } from '@shared/format';
import { api, download, enc, messageOf, upload } from '../../lib/api';
import { useMe } from '../../lib/me';
import { useToast } from '../../lib/toast';
import type { AdminUser, PlanSummary, ScheduleMessage } from '../../lib/types';
import { useTourHooks } from '../../tour';
import {
  Badge, Button, Callout, Card, Empty, Field, Input, PageHeader, Select, Spinner, StatusBadge, Textarea,
} from '../../ui';
import { DataTable, type Column } from '../../ui/DataTable';
import { Dialog } from '../../ui/Dialog';
import { Dropzone } from '../../ui/Dropzone';
import { Combobox } from '../../ui/Combobox';
import { Menu } from '../../ui/Menu';
import { useConfirm } from '../../ui/confirm';
import { NextStep } from '../../blocks/NextStep';

const PROFILE_FIELDS = ['full_name', 'position', 'department', 'phone', 'email', 'mentor', 'manager', 'plan_id', 'start_date', 'notes'] as const;
type Profile = Record<(typeof PROFILE_FIELDS)[number], string>;
const EMPTY: Profile = { full_name: '', position: '', department: '', phone: '', email: '', mentor: '', manager: '', plan_id: '', start_date: '', notes: '' };

const ROLE_CONFIRM: Record<string, string> = {
  owner: 'Сделать суперадмином? Полный доступ: все документы, все сотрудники, раздача прав.',
  admin: 'Сделать администратором? Доступ к документам, планам и сотрудникам своего подразделения.',
  employee: 'Сделать обычным сотрудником? Доступ к админке пропадёт.',
};

// ---------------------------------------------------------------- штатное расписание
type StaffRow = { full_name?: string; position?: string; department?: string; start_date?: string; exists?: boolean; foreign?: boolean };
const STAFF_KEYS: [keyof StaffRow, string][] = [['full_name', 'ФИО'], ['position', 'Должность'], ['department', 'Подразделение'], ['start_date', 'Дата выхода']];

function Staffing({ onDone }: { onDone: () => void }) {
  const [rows, setRows] = useState<StaffRow[]>([]);
  const [status, setStatus] = useState<{ tone: 'accent' | 'danger' | 'ok'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [skipped, setSkipped] = useState<{ full_name: string; reason: string }[]>([]);
  const { confirm } = useConfirm();
  const preview = async (file: File) => {
    setRows([]); setSkipped([]); setBusy(true);
    setStatus({ tone: 'accent', text: `ИИ разбирает «${file.name}»…` });
    try {
      const d = await upload<{ records: StaffRow[]; count: number }>('/staffing/preview', file);
      const recs = d.records || [];
      const named = recs.filter((r) => (r.full_name || '').trim()).length;
      const exists = recs.filter((r) => r.exists).length;
      setRows(recs);
      setStatus({ tone: 'accent', text: `Найдено строк: ${d.count} (с ФИО: ${named}, вакансий: ${d.count - named})${exists ? ` · уже есть в системе и будут пропущены: ${exists}` : ''}. Поля можно поправить.` });
    } catch (e) {
      setStatus({ tone: 'danger', text: messageOf(e) });
    } finally {
      setBusy(false);
    }
  };
  const fresh = rows.filter((r) => !r.exists && !r.foreign).length;
  const create = async () => {
    if (!rows.length || !(await confirm({ title: 'Создать записи из штатного расписания?', text: `Строк: ${rows.length}. Уже заведённые будут пропущены.`, ok: 'Создать' }))) return;
    setBusy(true);
    try {
      const d = await api.post<{ profiles: { id: string }[]; vacancies: unknown[]; skipped: { full_name: string; reason: string }[] }>(
        '/staffing/import', { records: rows.map(({ exists: _e, foreign: _f, ...r }) => r) });
      const profiles = d.profiles || [];
      setStatus({ tone: 'ok', text: `Профилей: ${profiles.length}, вакансий: ${(d.vacancies || []).length}, пропущено: ${(d.skipped || []).length}.`
        + (profiles.length ? ' Excel с логинами и паролями скачан — повторно его можно выгрузить кнопкой «Логины и пароли».' : '') });
      setSkipped(d.skipped || []);
      setRows([]);
      if (profiles.length) download(`/users/credentials.xlsx?ids=${enc(profiles.map((p) => p.id).join(','))}`, 'логины_и_пароли.xlsx');
      onDone();
    } catch (e) {
      setStatus({ tone: 'danger', text: messageOf(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <div className="nm-card-title" style={{ fontSize: 18 }}>Штатное расписание</div>
      <p className="nm-small nm-muted" style={{ margin: 0 }}>
        Загрузите штатное расписание (xlsx, xls, csv) — ИИ сам определит структуру. Строки с ФИО станут профилями сотрудников
        (логин из фамилии и инициалов, пароль), строки без ФИО — вакансиями. Уже заведённые люди и вакансии пропускаются.
      </p>
      <div data-tour="staffing-drop">
        <Dropzone accept=".xlsx,.xls,.csv,.tsv" icon={FileSpreadsheet} onFiles={(f) => preview(f[0])} disabled={busy}
                  title="Перетащите штатное расписание сюда или нажмите, чтобы выбрать" hint="Форматы .xlsx, .xls, .csv" />
      </div>
      {status && <Callout tone={status.tone} icon={busy ? undefined : status.tone === 'danger' ? TriangleAlert : undefined}>
        {busy && <span className="nm-spin" aria-hidden style={{ marginRight: 8, verticalAlign: '-4px' }} />}{status.text}</Callout>}
      {rows.length > 0 && (
        <>
          <div className="nm-row-between">
            <span className="nm-small nm-muted">Строки с ФИО станут профилями, без ФИО — вакансиями.</span>
            <Button variant="primary" icon={UserPlus} onClick={create} loading={busy} disabled={!fresh}>Создать ({fresh})</Button>
          </div>
          <div className="nm-table-wrap nm-glass">
            <table className="nm-edit-table">
              <thead><tr>{STAFF_KEYS.map(([, t]) => <th key={t}>{t}</th>)}<th /><th /></tr></thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} data-muted={r.exists || r.foreign || undefined}>
                    {STAFF_KEYS.map(([k, t]) => (
                      <td key={k}><input className="nm-input nm-input-sm" aria-label={t} value={(r[k] as string) || ''}
                                         onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)))} /></td>
                    ))}
                    <td className="nm-micro nm-warn-text" style={{ whiteSpace: 'nowrap' }}>{r.exists ? 'уже есть' : r.foreign ? 'другое подразделение' : ''}</td>
                    <td><Button size="sm" variant="ghost" iconOnly icon={X} aria-label="Убрать строку" onClick={() => setRows(rows.filter((_, j) => j !== i))} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {skipped.length > 0 && (
        <details className="nm-small">
          <summary style={{ cursor: 'pointer' }}>Пропущены ({skipped.length})</summary>
          {skipped.map((s, i) => <div key={i} className="nm-muted">{s.full_name} — {s.reason}</div>)}
        </details>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- карточка сотрудника
function UserDialog({ open, user, users, plans, readyProfs, defaultPlanId, onClose, onSaved, demo }: {
  open: boolean; user: AdminUser | null; users: AdminUser[]; plans: PlanSummary[]; readyProfs: Set<string>;
  defaultPlanId: string | null; onClose: () => void; onSaved: (created?: { username: string; temp_password: string }) => void; demo?: boolean;
}) {
  const { me, isOwner } = useMe();
  const toast = useToast();
  const { confirm } = useConfirm();
  const [f, setF] = useState<Profile>(EMPTY);
  const [role, setRole] = useState<string>('employee');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(readyProfs);
  useEffect(() => setReady(readyProfs), [readyProfs]);
  useEffect(() => {
    if (!open) return;
    const base = { ...EMPTY };
    if (user) PROFILE_FIELDS.forEach((k) => { base[k] = String((user as Record<string, unknown>)[k] ?? ''); });
    else if (!isOwner) base.department = me?.department || '';
    setF(base);
    setRole(user?.role || 'employee');
    setError(null);
  }, [open, user, isOwner, me]);
  const set = (k: keyof Profile) => (v: string) => setF((x) => ({ ...x, [k]: v }));
  const positions = useMemo(() => [...new Set(users.map((u) => (u.position || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru')), [users]);
  const departments = useMemo(() => [...new Set(users.map((u) => (u.department || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru')), [users]);
  const people = useMemo(() => [...new Set(users.filter((u) => u.id !== user?.id && !(u.full_name || '').startsWith('(вакансия)'))
    .map((u) => (u.full_name || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru')), [users, user]);
  const showRole = isOwner && !!user && user.id !== me?.id;

  const deleteProf = async (prof: string) => {
    if (!defaultPlanId) { toast.warn('Общий план не назначен — удалять нечего.'); return; }
    if (!(await confirm({ title: 'Удалить сообщения для должности?', text: `Сгенерированные сообщения плана для «${prof}» будут удалены.`, ok: 'Удалить', danger: true }))) return;
    try {
      await api.del(`/plans/${enc(defaultPlanId)}/schedule?profession=${enc(prof)}`);
      setReady((s) => { const n = new Set(s); n.delete(prof); return n; });
    } catch (e) { toast.error(messageOf(e)); }
  };

  const save = async () => {
    if (demo) { onClose(); return; }
    if (!f.full_name.trim()) { setError('Укажите ФИО'); return; }
    setBusy(true);
    setError(null);
    const payload = Object.fromEntries(PROFILE_FIELDS.map((k) => [k, f[k] || null]));
    try {
      const saved = await (user ? api.put<AdminUser>(`/users/${enc(user.id)}`, payload) : api.post<AdminUser>('/users', payload));
      if (showRole && user && role !== user.role && (await confirm({ title: 'Сменить роль?', text: ROLE_CONFIRM[role], ok: 'Сменить' }))) {
        try { await api.post(`/users/${enc(user.id)}/role`, { role }); } catch (e) { toast.error(`Роль не изменена: ${messageOf(e)}`); }
      }
      onSaved(user ? undefined : { username: saved.username || '', temp_password: saved.temp_password || '' });
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} wide modal={!demo} className={demo ? 'nmt-demo' : undefined}
            title={user ? `Редактирование: ${user.full_name}` : 'Новый сотрудник'}
            subtitle={user ? undefined : 'Логин создастся из ФИО, пароль — автоматически.'}
            footer={<><Button variant="ghost" onClick={onClose}>Отмена</Button><Button variant="primary" loading={busy} onClick={save}>Сохранить</Button></>}>
      <form className="nm-stack" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); save(); }}>
        {error && <Callout tone="danger">{error}</Callout>}
        <div className="nm-field-row">
          <div data-tour="emp-name"><Field label="ФИО *"><Input value={f.full_name} onChange={(e) => set('full_name')(e.target.value)} placeholder="Иванов Иван Иванович" autoFocus={!demo} /></Field></div>
          <Field label="Логин" help="Создаётся один раз из фамилии и инициалов и не меняется. Пароль выдаётся автоматически и виден в списке, пока сотрудник не задаст свой.">
            <Input value={user?.username || ''} disabled placeholder="создастся из ФИО" />
          </Field>
          <Field label="Должность" help="Выбор из штатного расписания или введите свою. ✓ — для должности уже сгенерированы сообщения плана.">
            <Combobox value={f.position} onChange={set('position')} options={positions} free placeholder="Из штатного расписания или своя"
                      marked={ready} markTitle="Сообщения плана готовы" onDelete={deleteProf} />
          </Field>
          <Field label="Подразделение" help={isOwner ? 'Справочник строится из штатного расписания. Новое подразделение можно добавить, набрав название.' : 'Подразделение меняет суперадмин'}>
            <Combobox value={f.department} onChange={set('department')} options={departments} free disabled={!isOwner} placeholder="Выбор из справочника" />
          </Field>
          <div data-tour="emp-plan">
            <Field label="План адаптации">
              <Select value={f.plan_id} onChange={(e) => set('plan_id')(e.target.value)}>
                <option value="">— план не назначен —</option>
                {plans.map((p) => <option key={p.plan_id} value={p.plan_id}>{p.generated ? '✓ ' : ''}{p.title}{p.generated ? ' — сообщения готовы' : ' — без сообщений'}</option>)}
              </Select>
            </Field>
          </div>
          <Field label="Дата выхода на работу" help="По дате выхода статус меняется сам: до неё — «Ждёт выхода», во время плана — «Проходит адаптацию», после — «Завершил».">
            <Input type="date" value={f.start_date} onChange={(e) => set('start_date')(e.target.value)} />
          </Field>
          <div data-tour="emp-mentor"><Field label="Наставник"><Combobox value={f.mentor} onChange={set('mentor')} options={people} placeholder="Выбор из сотрудников" /></Field></div>
          <Field label="Руководитель"><Combobox value={f.manager} onChange={set('manager')} options={people} placeholder="Выбор из сотрудников" /></Field>
          <Field label="Телефон"><Input type="tel" value={f.phone} onChange={(e) => set('phone')(e.target.value)} placeholder="+7 900 000-00-00" /></Field>
          <Field label="Email"><Input type="email" value={f.email} onChange={(e) => set('email')(e.target.value)} placeholder="ivanov@company.ru" /></Field>
          {showRole && (
            <Field label="Роль" help="Администратор — документы, планы и сотрудники своего подразделения. Суперадмин — всё и все.">
              <Select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="employee">Сотрудник</option><option value="admin">Администратор</option><option value="owner">Суперадмин</option>
              </Select>
            </Field>
          )}
        </div>
        <Field label="Заметки"><Textarea value={f.notes} onChange={(e) => set('notes')(e.target.value)} placeholder="Любые пометки по сотруднику" style={{ minHeight: 80 }} /></Field>
      </form>
    </Dialog>
  );
}

function CredentialsDialog({ user, onClose, onChanged }: { user: AdminUser | null; onClose: () => void; onChanged: () => void }) {
  const [pw, setPw] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ username: string; temp_password: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPw(''); setError(null); setResult(null); }, [user]);
  const submit = async () => {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.post(`/users/${enc(user.id)}/credentials`, { password: pw || null }));
      onChanged();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  };
  return (
    <Dialog open={!!user} onClose={onClose} title={`Доступ: ${user?.full_name || ''}`}
            subtitle="Старый пароль перестанет действовать, сотрудника разлогинит на всех устройствах."
            footer={<><Button variant="ghost" onClick={onClose}>Закрыть</Button>{!result && <Button variant="primary" loading={busy} onClick={submit}>Выдать новый пароль</Button>}</>}>
      <div>Логин: <b>{user?.username || 'создастся из ФИО'}</b></div>
      {!result && <Field label="Новый пароль" error={error}><Input value={pw} onChange={(e) => setPw(e.target.value)} placeholder="пусто — сгенерируем автоматически" /></Field>}
      {result && <Callout tone="ok" icon={Check}>Логин: <b>{result.username}</b> · пароль: <code className="nm-code">{result.temp_password}</code></Callout>}
    </Dialog>
  );
}

type UserSchedule = {
  employee: { full_name: string }; plan_title: string; start_date: string; plan_generated: boolean; messages: ScheduleMessage[];
  paused?: boolean; pauses?: { start: string; end: string | null }[];
};

function ScheduleDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const [data, setData] = useState<UserSchedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    if (user) api.get<UserSchedule>(`/users/${enc(user.id)}/schedule`).then(setData).catch((e) => setError(messageOf(e)));
  }, [user]);
  const base = user ? `/users/${enc(user.id)}/export` : '';
  return (
    <Dialog open={!!user} onClose={onClose} wide title={`Расписание: ${user?.full_name || ''}`}
            subtitle={data ? `План «${data.plan_title}» · выход ${ruDate(data.start_date)} · сообщений: ${data.messages.length}` : undefined}
            footer={data ? <><a className="nm-btn nm-btn-secondary" href={`${base}/schedule.md`} download><Download aria-hidden />schedule.md</a>
              <a className="nm-btn nm-btn-secondary" href={`${base}/schedule.json`} download><Download aria-hidden />schedule.json</a></> : undefined}>
      {error && <Callout tone="warn" icon={TriangleAlert}>{error}</Callout>}
      {!data && !error && <Spinner />}
      {data?.paused && <Callout tone="warn" icon={Pause}>Сотрудник на больничном: план стоит. После возобновления он продолжится с того же места — даты ниже сдвинутся на срок паузы.</Callout>}
      {data && !data.paused && !!data.pauses?.length && <Callout icon={Clock}>Даты сдвинуты на больничные ({data.pauses.length}): план продолжился с того места, где сотрудник остановился.</Callout>}
      {data && !data.plan_generated && <Callout tone="warn" icon={TriangleAlert}>У плана «{data.plan_title}» ещё нет сообщений. Даты рассчитаны, тексты пустые — обновите сообщения в разделе «Сообщения».</Callout>}
      {data && (
        <div className="nm-stack">
          {data.messages.map((m) => (
            <Card key={m.message_id} style={{ gap: 4 }}>
              <div className="nm-card-kicker"><span>{m.stage.title} · {m.substage.title}</span>
                <time title={m.schedule.planned_at ? `По плану: ${m.schedule.planned_at.replace('T', ' ')}` : undefined}>
                  {m.schedule.send_at ? m.schedule.send_at.replace('T', ' ') : `${m.schedule.offset_days >= 0 ? '+' : ''}${m.schedule.offset_days} дн., ${m.schedule.time}`}
                  {m.schedule.planned_at && <span className="nm-muted"> (сдвинуто)</span>}</time></div>
              <div className="nm-small nm-pre">{m.content.text || '—'}</div>
              {m.error && <div className="nm-micro nm-danger-text">{m.error}</div>}
            </Card>
          ))}
        </div>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------- страница
export default function Users() {
  const { me, isOwner } = useMe();
  const toast = useToast();
  const { confirm } = useConfirm();
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [defaultPlanId, setDefaultPlanId] = useState<string | null>(null);
  const [readyProfs, setReadyProfs] = useState<Set<string>>(new Set());
  const [dept, setDept] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [staffing, setStaffing] = useState(false);
  const [editing, setEditing] = useState<{ user: AdminUser | null; demo?: boolean } | null>(null);
  const [created, setCreated] = useState<{ username: string; temp_password: string } | null>(null);
  const [creds, setCreds] = useState<AdminUser | null>(null);
  const [schedule, setSchedule] = useState<AdminUser | null>(null);

  useEffect(() => { document.title = 'Пользователи · НейроМастер'; }, []);
  const load = useCallback(() => api.get<{ users: AdminUser[] }>('/users').then((d) => setUsers(d.users || [])).catch((e) => toast.error(messageOf(e))), [toast]);
  useEffect(() => {
    load();
    api.get<{ plans: PlanSummary[]; default_plan_id?: string }>('/plans').then((d) => {
      setPlans(d.plans || []);
      setDefaultPlanId(d.default_plan_id || null);
      if (d.default_plan_id) {
        api.get<{ generated_names?: string[] }>(`/plans/${enc(d.default_plan_id)}/professions`)
          .then((p) => setReadyProfs(new Set(p.generated_names || []))).catch(() => {});
      }
    }).catch(() => {});
  }, [load]);

  useTourHooks('users', {
    setStaffing,
    openDemo: () => setEditing((e) => e || { user: null, demo: true }),
    closeDemo: () => setEditing((e) => (e?.demo ? null : e)),
  });

  const list = useMemo(() => users || [], [users]);
  const departments = useMemo(() => [...new Set(list.map((u) => (u.department || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru')), [list]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return list.filter((u) => (!dept || (u.department || '').trim() === dept)
      && (!q || [u.full_name, u.username, u.position, u.department].some((v) => (v || '').toLowerCase().includes(q))));
  }, [list, dept, query]);
  const pending = list.filter((u) => !u.active);
  const canManage = (u: AdminUser) => isOwner || u.role === 'employee';
  const selectable = shown.filter((u) => canManage(u) && u.id !== me?.id);
  const allOn = selectable.length > 0 && selectable.every((u) => selected.has(u.id));
  useEffect(() => {
    setSelected((s) => { const n = [...s].filter((id) => shown.some((u) => u.id === id)); return n.length === s.size ? s : new Set(n); });
  }, [shown]);

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) toast.ok(ok); load(); } catch (e) { toast.error(messageOf(e)); }
  };
  const remove = async (u: AdminUser) => {
    if (await confirm({ title: 'Удалить пользователя?', text: `«${u.full_name}» будет удалён безвозвратно.`, ok: 'Удалить', danger: true })) {
      act(() => api.del(`/users/${enc(u.id)}`), 'Пользователь удалён');
    }
  };
  const pause = async (u: AdminUser, paused: boolean) => {
    if (paused && !(await confirm({ title: 'Приостановить адаптацию?', text: `План «${u.full_name}» встанет на паузу. После возобновления продолжится с того места, где остановился: даты сообщений сдвинутся на срок паузы.`, ok: 'Приостановить' }))) return;
    act(() => api.post(`/users/${enc(u.id)}/pause`, { paused }), paused ? 'Адаптация приостановлена' : 'Адаптация возобновлена — план продолжится с того же места');
  };
  const bulkDelete = async () => {
    const names = list.filter((u) => selected.has(u.id)).map((u) => u.full_name);
    if (!names.length) return;
    const preview = names.slice(0, 10).join('\n') + (names.length > 10 ? `\n…и ещё ${names.length - 10}` : '');
    if (!(await confirm({ title: `Удалить ${names.length} пользователей?`, text: `Безвозвратно:\n${preview}`, ok: 'Удалить', danger: true }))) return;
    try {
      const d = await api.post<{ deleted: number; skipped?: { full_name: string; reason: string }[] }>('/users/bulk-delete', { ids: [...selected] });
      const skipped = (d.skipped || []).map((s) => `${s.full_name} — ${s.reason}`).join('\n');
      toast.push({ tone: skipped ? 'warn' : 'ok', text: `Удалено: ${d.deleted}.${skipped ? `\nНе удалены:\n${skipped}` : ''}` });
      setSelected(new Set());
      load();
    } catch (e) { toast.error(messageOf(e)); }
  };

  const columns: Column<AdminUser>[] = [
    { key: 'sel', width: '28px', title: <input type="checkbox" aria-label="Отметить всех" checked={allOn} disabled={!selectable.length}
        onChange={(e) => setSelected(e.target.checked ? new Set(selectable.map((u) => u.id)) : new Set())} />,
      render: (u) => (canManage(u) && u.id !== me?.id
        ? <input type="checkbox" aria-label={`Отметить ${u.full_name}`} checked={selected.has(u.id)}
                 onChange={(e) => setSelected((s) => { const n = new Set(s); if (e.target.checked) n.add(u.id); else n.delete(u.id); return n; })} />
        : null) },
    { key: 'user', width: 'minmax(220px, 2.2fr)', title: 'Пользователь', render: (u) => (
      <>
        <div className="nm-cell-title">{u.full_name}</div>
        <div className="nm-cell-sub">{u.username ? <>@{u.username}{u.temp_password && <> · пароль: <code className="nm-code">{u.temp_password}</code></>}</>
          : <span className="nm-warn-text">нет логина — выдайте доступ</span>}</div>
        <div className="nm-cell-sub">{[u.position, u.department].filter(Boolean).join(' · ') || '—'}{!u.active && <span className="nm-warn-text"> · не подтверждён</span>}</div>
      </>
    ) },
    { key: 'role', width: '130px', title: 'Роль', render: (u) => <StatusBadge view={ROLE[u.role] || ROLE.employee} /> },
    { key: 'plan', width: 'minmax(150px, 1.3fr)', title: 'План адаптации', render: (u) => (
      u.plan_title ? <><div className="nm-cell-title" style={{ fontWeight: 500 }}>{u.plan_title}</div>
        {!u.plan_generated && <div className="nm-cell-sub nm-warn-text">сообщения не сгенерированы</div>}</>
        : u.role === 'employee' ? <span className="nm-danger-text">не назначен</span> : <span className="nm-muted">—</span>
    ) },
    { key: 'start', width: '100px', title: 'Дата выхода', render: (u) => <span className="nm-muted">{ruDate(u.start_date)}</span> },
    { key: 'status', width: '160px', title: 'Статус', render: (u) => (u.role === 'employee'
      ? <StatusBadge view={employeeStatus(u.status, !!u.plan_id, u.active)} /> : !u.active ? <Badge tone="warn">Ждёт подтверждения</Badge> : null) },
    { key: 'actions', width: '160px', title: '', render: (u) => {
      const self = u.id === me?.id;
      const manage = canManage(u);
      return (
        <div className="nm-cell-actions" data-tour="user-actions">
          {manage && <Button size="sm" icon={Pencil} onClick={() => setEditing({ user: u })}>Изменить</Button>}
          <Menu label={`Действия: ${u.full_name}`} items={[
            { label: 'Расписание', icon: CalendarDays, onClick: () => setSchedule(u) },
            { label: 'Доступ (новый пароль)', icon: KeyRound, onClick: () => setCreds(u), hidden: !manage },
            { label: 'Приостановить (больничный)', icon: Pause, onClick: () => pause(u, true), hidden: !manage || u.role !== 'employee' || !u.plan_id || u.status === 'paused' },
            { label: 'Возобновить', icon: Play, onClick: () => pause(u, false), hidden: !manage || u.role !== 'employee' || !u.plan_id || u.status !== 'paused' },
            { label: 'Заблокировать вход', icon: Ban, onClick: () => act(() => api.post(`/users/${enc(u.id)}/active`, { active: false }), 'Вход заблокирован'), hidden: !manage || self || !u.active },
            { label: 'Подтвердить', icon: Check, onClick: () => act(() => api.post(`/users/${enc(u.id)}/active`, { active: true }), 'Учётная запись подтверждена'), hidden: !manage || self || u.active },
            { separator: true, hidden: !manage || self },
            { label: 'Удалить', icon: Trash2, onClick: () => remove(u), danger: true, hidden: !manage || self },
          ]} trigger={(p) => <Button size="sm" variant="ghost" iconOnly icon={MoreHorizontal} aria-label="Ещё действия" {...p} />} />
        </div>
      );
    } },
  ];

  return (
    <div className="nm-page">
      <PageHeader title="Пользователи" subtitle="Сотрудники на адаптации, наставники и администраторы."
                  actions={<>
                    <Button variant="primary" icon={FileSpreadsheet} onClick={() => setStaffing((v) => !v)} data-tour="btn-staffing" aria-expanded={staffing}>Загрузить штатное расписание</Button>
                    <Button icon={UserPlus} onClick={() => setEditing({ user: null })} data-tour="btn-add-user">Добавить сотрудника</Button>
                  </>} />
      <NextStep here="/admin/users" />
      {staffing && <Staffing onDone={load} />}
      {pending.length > 0 && (
        <Callout tone="warn" icon={Clock}>Ждут подтверждения: {pending.map((u) => u.full_name).join(', ')}. Пока учётная запись не подтверждена, войти нельзя.</Callout>
      )}
      <div className="nm-row" data-tour="users-toolbar">
        <div className="nm-search nm-grow" style={{ minWidth: 220, maxWidth: 360 }}>
          <Search aria-hidden />
          <Input small aria-label="Поиск" placeholder="Поиск по ФИО, логину, должности" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <Select small aria-label="Подразделение" value={dept} onChange={(e) => setDept(e.target.value)} style={{ width: 'auto', minWidth: 200 }}>
          <option value="">Все подразделения</option>
          {departments.map((d) => <option key={d} value={d}>{d}</option>)}
        </Select>
        <a className="nm-btn nm-btn-secondary nm-btn-sm" href="/users/credentials.xlsx" download title="Логины и пароли сотрудников, ещё не входивших в систему">
          <Download aria-hidden />Логины и пароли (Excel)</a>
        {selected.size > 0 && <Button size="sm" variant="danger" icon={UserX} onClick={bulkDelete}>Удалить отмеченных ({selected.size})</Button>}
      </div>
      {users === null ? <Spinner /> : (
        <DataTable label="Пользователи" columns={columns} rows={shown} rowKey={(u) => u.id}
                   empty={<Empty icon={UsersIcon}>{list.length ? 'В этом подразделении никого нет.' : 'Пользователей пока нет — загрузите штатное расписание или добавьте сотрудника вручную.'}</Empty>} />
      )}
      <UserDialog open={!!editing} user={editing?.user || null} demo={editing?.demo} users={list} plans={plans} readyProfs={readyProfs} defaultPlanId={defaultPlanId}
                  onClose={() => setEditing(null)}
                  onSaved={(c) => { setEditing(null); if (c) setCreated(c); else toast.ok('Сохранено'); load(); }} />
      <Dialog open={!!created} onClose={() => setCreated(null)} title="Сотрудник создан"
              footer={<Button variant="primary" onClick={() => setCreated(null)}>Готово</Button>}>
        <Callout tone="ok" icon={Check}>Логин: <b>{created?.username}</b> · пароль: <code className="nm-code">{created?.temp_password}</code></Callout>
        <p className="nm-small nm-muted" style={{ margin: 0 }}>Пароль виден в списке и в Excel, пока сотрудник не задаст свой. Сменить его можно в меню «⋯ → Доступ».</p>
      </Dialog>
      <CredentialsDialog user={creds} onClose={() => setCreds(null)} onChanged={load} />
      <ScheduleDialog user={schedule} onClose={() => setSchedule(null)} />
    </div>
  );
}
