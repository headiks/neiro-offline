// Вопросы сотрудников, на которые ассистент не ответил сам (нет в документах или похоже на
// ЧС). Слева очередь (ЧС сверху), справа карточка ответа — ответ уходит сотруднику в кабинет
// и пушем. Вкладка «База ответов» — готовые ответы, которые ассистент выдаёт мгновенно.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { CircleCheck, Link2, MessageCircleQuestion, RefreshCw, SendHorizontal, Siren } from 'lucide-react';
import { ago, initials, ruDateTime } from '@shared/format';
import { QUESTION_STATUS } from '@shared/status';
import { api, enc, messageOf } from '../../lib/api';
import { usePolling } from '../../lib/poll';
import { useToast } from '../../lib/toast';
import type { AdminQuestion } from '../../lib/types';
import { Avatar, Badge, Button, Callout, Card, Checkbox, Empty, Field, Input, PageHeader, Segmented, Spinner, StatusBadge, Textarea } from '../../ui';
import QaBase from './QaBase';
import { DataTable, type Column } from '../../ui/DataTable';

type View = 'open' | 'resolved' | 'all' | 'base';
const isSos = (q: AdminQuestion) => q.reason === 'escalate';

function AnswerPanel({ q, onDone }: { q: AdminQuestion; onDone: () => void }) {
  const [answer, setAnswer] = useState('');
  const [toBase, setToBase] = useState(true);
  const [baseQuestion, setBaseQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  // Ответ на ЧС — про конкретный случай, в общую базу по умолчанию не кладём.
  useEffect(() => { setAnswer(''); setToBase(!isSos(q)); setBaseQuestion(q.resolved_question || q.question); }, [q.id]);
  const send = async () => {
    if (!answer.trim()) return;
    setBusy(true);
    try {
      await api.post(`/questions/${enc(q.id)}/resolve`, { answer: answer.trim(), add_to_base: toBase, base_question: baseQuestion.trim() || null });
      toast.ok('Ответ отправлен сотруднику');
      onDone();
    } catch (e) { toast.error(messageOf(e)); } finally { setBusy(false); }
  };
  const who = [q.user_name, q.position].filter(Boolean).join(', ');
  return (
    <Card className="nm-answer-panel nm-sticky">
      <div className="nm-row">
        <Avatar text={initials(q.user_name)} />
        <div className="nm-grow">
          <div className="nm-cell-title">{q.user_name || 'Сотрудник'}</div>
          <div className="nm-cell-sub">{[q.position, q.department].filter(Boolean).join(' · ') || '—'}</div>
        </div>
        {isSos(q) ? <Badge tone="danger" icon={Siren}>ЧС</Badge> : <StatusBadge view={QUESTION_STATUS[q.status]} />}
      </div>
      <h2>{q.question}</h2>
      <div className="nm-context" data-tone={isSos(q) ? 'danger' : undefined}>
        {isSos(q) ? 'Похоже на чрезвычайную ситуацию — ответьте как можно скорее и при необходимости свяжитесь с сотрудником.' : 'В документах ответа не нашлось.'}
        {q.resolved_question && q.resolved_question !== q.question && <div style={{ marginTop: 6 }}><Link2 aria-hidden style={{ width: 14, height: 14, verticalAlign: '-2px' }} /> Понято как: «{q.resolved_question}»</div>}
        <div style={{ marginTop: 6 }} className="nm-muted">
          Задан {ruDateTime(q.created_at)}{q.contact ? ` · ${q.contact}` : ''}{q.mentor ? ` · наставник: ${q.mentor}` : ''}
        </div>
      </div>
      {q.status === 'resolved' ? (
        <>
          <Callout tone="ok" icon={CircleCheck}><div className="nm-pre">{q.answer}</div></Callout>
          <div className="nm-micro nm-muted">Ответил: {q.answered_by || '—'} · {ruDateTime(q.answered_at)}</div>
        </>
      ) : (
        <>
          <Field label={`Ответ ${who ? `для ${who}` : 'сотруднику'}`}>
            <Textarea value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Ответ сотруднику (можно после консультации со специалистом)" />
          </Field>
          <Checkbox label="Добавить в базу ответов — похожий вопрос получит этот ответ сразу" checked={toBase} onChange={setToBase} />
          {toBase && (
            <Field label="Вопрос для базы" help="Без имён и личных подробностей — так, как его задал бы любой сотрудник">
              <Input value={baseQuestion} onChange={(e) => setBaseQuestion(e.target.value)} />
            </Field>
          )}
          <Button variant="primary" size="lg" icon={SendHorizontal} loading={busy} disabled={!answer.trim()} onClick={send}>Отправить ответ</Button>
        </>
      )}
    </Card>
  );
}

export default function Questions() {
  const [view, setView] = useState<View>('open');
  const [items, setItems] = useState<AdminQuestion[] | null>(null);
  const [openCount, setOpenCount] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => { document.title = 'Вопросы · НейроМастер'; }, []);
  const load = useCallback(() => {
    api.get<{ questions: AdminQuestion[]; open_count?: number }>(`/questions?status=${view === 'all' ? '' : view}`).then((d) => {
      setItems(d.questions || []);
      setOpenCount(d.open_count || 0);
    }).catch(() => setItems((x) => x || []));
  }, [view]);
  usePolling(() => { if (view !== 'base') load(); }, 30000, [view]);
  useEffect(() => { setItems(null); setSelected(null); }, [view]);

  const rows = useMemo(() => (items || []).slice().sort((a, b) =>
    (Number(isSos(b) && b.status === 'open') - Number(isSos(a) && a.status === 'open')) || String(b.created_at).localeCompare(String(a.created_at))), [items]);
  const current = rows.find((q) => q.id === selected) || rows[0] || null;

  const columns: Column<AdminQuestion>[] = [
    { key: 'who', width: 'minmax(150px, 1fr)', title: 'Сотрудник', render: (q) => <><div className="nm-cell-title">{q.user_name || '—'}</div><div className="nm-cell-sub">{q.position || ''}</div></> },
    { key: 'q', width: 'minmax(200px, 2fr)', title: 'Вопрос', render: (q) => (
      <div className="nm-row" style={{ gap: 6, flexWrap: 'nowrap' }}>
        {isSos(q) && <Badge tone="danger">ЧС</Badge>}
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>{q.question}</span>
      </div>
    ) },
    { key: 'when', width: '110px', title: 'Когда', render: (q) => <span className="nm-muted">{ago(q.created_at)}</span> },
  ];
  if (view !== 'open') columns.push({ key: 'st', width: '110px', title: 'Статус', render: (q) => <StatusBadge view={QUESTION_STATUS[q.status]} /> });

  return (
    <div className="nm-page">
      <PageHeader title="Вопросы сотрудников" subtitle="Сюда попадают вопросы с пометкой ЧС и те, на которые в регламентах не нашлось ответа. Ответ придёт сотруднику в кабинет."
                  actions={<Button variant="ghost" icon={RefreshCw} onClick={load}>Обновить</Button>} />
      <Segmented label="Какие вопросы" value={view} onChange={setView}
                 options={[{ value: 'open', label: `Открытые${openCount ? ` · ${openCount}` : ''}` }, { value: 'resolved', label: 'Отвечено' }, { value: 'all', label: 'Все' }, { value: 'base', label: 'База ответов' }]} />
      {view === 'base' ? <QaBase /> : items === null ? <Spinner /> : !rows.length ? (
        <Card pad={false}><Empty icon={view === 'open' ? CircleCheck : MessageCircleQuestion}>{view === 'open' ? 'Открытых вопросов нет — ассистент справляется сам.' : 'Вопросов нет.'}</Empty></Card>
      ) : (
        <div className="nm-two-pane">
          <DataTable label="Вопросы" columns={columns} rows={rows} rowKey={(q) => q.id} selectedKey={current?.id} onRowClick={(q) => setSelected(q.id)} />
          {current && <AnswerPanel q={current} onDone={() => { setSelected(null); load(); }} />}
        </div>
      )}
    </div>
  );
}
