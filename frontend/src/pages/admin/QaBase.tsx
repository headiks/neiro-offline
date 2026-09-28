// База готовых ответов: частые вопросы по подэтапам и секциям (пишет модель по документам),
// ответы ассистента и специалистов. Похожий вопрос сотрудника получает ответ отсюда мгновенно,
// поэтому здесь правят неточные ответы и удаляют лишние. Правленый ответ считается ответом
// специалиста — автообновление по документам его не перезапишет.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { BookOpenCheck, RefreshCw, Save, Sparkles, Trash2 } from 'lucide-react';
import { ruDateTime } from '@shared/format';
import { api, messageOf } from '../../lib/api';
import { useToast } from '../../lib/toast';
import { useConfirm } from '../../ui/confirm';
import { Badge, Button, Card, Empty, Field, Input, Segmented, Spinner, Textarea } from '../../ui';
import { DataTable, type Column } from '../../ui/DataTable';

type Source = 'faq' | 'section' | 'model' | 'human';
type QaItem = { id: number; question: string; answer: string; source: Source; position: string; hits: number; updated_at: string };
type Filter = '' | Source;

const SOURCE_LABEL: Record<Source, string> = { faq: 'Частый вопрос', section: 'По документу', model: 'Ответ ассистента', human: 'Специалист' };
const SOURCE_TONE: Record<Source, 'accent' | 'muted' | 'ok'> = { faq: 'accent', section: 'muted', model: 'muted', human: 'ok' };

function Editor({ item, onSaved }: { item: QaItem; onSaved: () => void }) {
  const [question, setQuestion] = useState(item.question);
  const [answer, setAnswer] = useState(item.answer);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const { confirm } = useConfirm();
  useEffect(() => { setQuestion(item.question); setAnswer(item.answer); }, [item.id]);
  const changed = question.trim() !== item.question || answer.trim() !== item.answer;
  const save = async () => {
    setBusy(true);
    try { await api.put(`/qa-base/${item.id}`, { question: question.trim(), answer: answer.trim() }); toast.ok('Ответ сохранён'); onSaved(); }
    catch (e) { toast.error(messageOf(e)); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!(await confirm({ title: 'Удалить ответ из базы?', text: 'Похожие вопросы снова пойдут к ассистенту или специалисту.', ok: 'Удалить', danger: true }))) return;
    try { await api.del(`/qa-base/${item.id}`); toast.ok('Ответ удалён'); onSaved(); } catch (e) { toast.error(messageOf(e)); }
  };
  return (
    <Card className="nm-answer-panel nm-sticky">
      <div className="nm-row">
        <Badge tone={SOURCE_TONE[item.source]}>{SOURCE_LABEL[item.source]}</Badge>
        {item.position && <Badge>{item.position}</Badge>}
        <span className="nm-grow" />
        <span className="nm-micro nm-muted">Выдан {item.hits} раз · {ruDateTime(item.updated_at)}</span>
      </div>
      <Field label="Вопрос"><Input value={question} onChange={(e) => setQuestion(e.target.value)} /></Field>
      <Field label="Ответ"><Textarea autosize value={answer} onChange={(e) => setAnswer(e.target.value)} /></Field>
      <div className="nm-row">
        <Button variant="primary" icon={Save} loading={busy} disabled={!changed || !question.trim() || !answer.trim()} onClick={save}>Сохранить</Button>
        <Button variant="ghost" icon={Trash2} onClick={remove}>Удалить</Button>
      </div>
    </Card>
  );
}

export default function QaBase() {
  const [filter, setFilter] = useState<Filter>('');
  const [items, setItems] = useState<QaItem[] | null>(null);
  const [stats, setStats] = useState<Partial<Record<Source, number>>>({});
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<number | null>(null);
  const toast = useToast();
  const load = useCallback(() => {
    api.get<{ items: QaItem[]; stats: Partial<Record<Source, number>> }>(`/qa-base?source=${filter}`)
      .then((d) => { setItems(d.items || []); setStats(d.stats || {}); })
      .catch((e) => { setItems((x) => x || []); toast.error(messageOf(e)); });
  }, [filter]);
  useEffect(() => { setItems(null); setSelected(null); load(); }, [load]);
  const refresh = async () => {
    try { await api.post('/qa-base/refresh', {}); toast.ok('Запущено: частые вопросы по новым и изменённым документам появятся здесь'); }
    catch (e) { toast.error(messageOf(e)); }
  };

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (items || []).filter((it) => !q || it.question.toLowerCase().includes(q) || it.answer.toLowerCase().includes(q));
  }, [items, query]);
  const current = rows.find((it) => it.id === selected) || rows[0] || null;
  const total = Object.values(stats).reduce((a, b) => a + (b || 0), 0);
  const count = (s: Source) => (stats[s] ? ` · ${stats[s]}` : '');

  const columns: Column<QaItem>[] = [
    { key: 'q', width: 'minmax(220px, 2fr)', title: 'Вопрос', render: (it) => (
      <><div className="nm-cell-title">{it.question}</div><div className="nm-cell-sub" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.answer}</div></>
    ) },
    { key: 'src', width: '150px', title: 'Источник', render: (it) => <Badge tone={SOURCE_TONE[it.source]}>{SOURCE_LABEL[it.source]}</Badge> },
    { key: 'hits', width: '80px', title: 'Выдан', render: (it) => <span className="nm-muted">{it.hits}</span> },
  ];

  return (
    <>
      <div className="nm-row" style={{ flexWrap: 'wrap' }}>
        <Segmented label="Источник ответа" value={filter} onChange={setFilter}
                   options={[{ value: '', label: `Все${total ? ` · ${total}` : ''}` }, { value: 'faq', label: `Частые${count('faq')}` },
                             { value: 'section', label: `По документам${count('section')}` }, { value: 'model', label: `Ассистент${count('model')}` },
                             { value: 'human', label: `Специалист${count('human')}` }]} />
        <span className="nm-grow" />
        <Button variant="ghost" icon={Sparkles} onClick={refresh}>Дополнить по документам</Button>
        <Button variant="ghost" icon={RefreshCw} onClick={load}>Обновить</Button>
      </div>
      <Input placeholder="Поиск по вопросу или ответу" value={query} onChange={(e) => setQuery(e.target.value)} />
      {items === null ? <Spinner /> : !rows.length ? (
        <Card pad={false}><Empty icon={BookOpenCheck}>{query ? 'Ничего не нашлось.' : 'Готовых ответов пока нет — они появятся после разбора документов.'}</Empty></Card>
      ) : (
        <div className="nm-two-pane">
          <DataTable label="Готовые ответы" columns={columns} rows={rows} rowKey={(it) => String(it.id)} selectedKey={current ? String(current.id) : undefined} onRowClick={(it) => setSelected(it.id)} />
          {current && <Editor item={current} onSaved={() => { setSelected(null); load(); }} />}
        </div>
      )}
    </>
  );
}
