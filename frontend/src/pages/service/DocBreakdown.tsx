// Разбор документа (docpipe): карточка документа и блоки — текст, этапы, подэтапы с
// уверенностью, профессии и обоснование «почему так размечено».
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { FileText, RefreshCw } from 'lucide-react';
import { api, enc, messageOf } from '../../lib/api';
import { Badge, Button, Callout, Card, Empty, Field, PageHeader, Select, Spinner } from '../../ui';

type Sub = { id?: string; title?: string; description?: string; confidence?: number };
// Фрагмент гибридного разбора: голоса алгоритмов за границу, ключевые слова, кандидаты и кто выбрал итог.
type Cand = { id: string; title: string; score: number; confidence: number };
type ChunkMeta = { votes?: Record<string, boolean>; boundary_score?: number | null; keywords?: [string, number][];
  candidates?: Cand[]; picked_by?: string };
type Chunk = { seq: number; text: string; substages?: { id: string; confidence?: number }[]; is_general?: boolean; meta?: ChunkMeta | null };
const VOTE: Record<string, string> = { texttiling: 'TextTiling', c99: 'C99', cues: 'разметка текста' };
const PICKED: Record<string, string> = { lexical: 'алгоритм (без ИИ)', local: 'локальная модель', deepseek: 'DeepSeek' };
type Section = { section_id?: string; heading_path?: string[]; page?: number; source?: string; is_meaningful?: boolean; reject_reason?: string;
  why?: string; stages?: { id?: string; title?: string; description?: string }[]; substages?: Sub[]; is_general?: boolean; professions?: string[]; prof_conf?: number; text?: string;
  chunks?: Chunk[] };
type Breakdown = { filename: string; doc_card?: { summary?: string; doc_type?: string; scope?: string; audience?: string[] }; sections?: Section[] };

export default function DocBreakdown() {
  const [params, setParams] = useSearchParams();
  const file = params.get('filename') || '';
  const [docs, setDocs] = useState<string[] | null>(null);
  const [data, setData] = useState<Breakdown | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { document.title = 'Разбор документа · НейроМастер'; api.get<{ documents: string[] }>('/documents/labeled').then((d) => setDocs((d.documents || []).filter(Boolean))).catch(() => setDocs([])); }, []);
  const load = useCallback(() => {
    setData(undefined); setError(null);
    if (!file) { setData(null); return; }
    api.get<Breakdown>(`/documents/${enc(file)}/labels`).then(setData).catch((e) => { setData(null); setError(messageOf(e)); });
  }, [file]);
  useEffect(load, [load]);
  const c = data?.doc_card || {};
  return (
    <div className="nm-page">
      <PageHeader title="Разбор документа" subtitle="Блок — раздел документа по заголовкам. Фрагменты и темы находят алгоритмы без ИИ (TextTiling, C99, разметка текста, TF-IDF); модель только выбирает из их кандидатов в спорных случаях." />
      <Card>
        <div className="nm-row" style={{ alignItems: 'flex-end' }}>
          <Field label="Документ (размеченные)" className="nm-grow">
            <Select value={file} onChange={(e) => setParams(e.target.value ? { filename: e.target.value } : {})}>
              <option value="">— выберите документ —</option>
              {(docs || []).map((f) => <option key={f} value={f}>{f}</option>)}
              {file && docs && !docs.includes(file) && <option value={file}>{file}</option>}
            </Select>
          </Field>
          <Button variant="ghost" icon={RefreshCw} onClick={load}>Обновить</Button>
        </div>
      </Card>
      {error && <Callout tone="warn">{error}</Callout>}
      {file && data === undefined && <Spinner />}
      {!file && <Card pad={false}><Empty icon={FileText}>{docs && !docs.length ? 'Нет размеченных документов.' : 'Выберите документ.'}</Empty></Card>}
      {data && (
        <>
          <Card>
            <div className="nm-card-title" style={{ fontSize: 18 }}>{data.filename}</div>
            <div className="nm-small">{c.summary || '—'}</div>
            <div className="nm-row" style={{ gap: 6 }}>
              <Badge tone="muted">тип: {c.doc_type || '—'}</Badge><Badge tone="muted">охват: {c.scope || '—'}</Badge><Badge tone="muted">аудитория: {(c.audience || []).join(', ') || '—'}</Badge>
            </div>
          </Card>
          <div className="nm-section-label">Блоков: {(data.sections || []).length}</div>
          {(data.sections || []).map((s, i) => (
            <Card key={s.section_id || i} style={s.is_meaningful === false ? { opacity: 0.7 } : undefined}>
              <div className="nm-card-kicker"><span>Блок {i + 1}{s.heading_path?.length ? `: ${s.heading_path.join(' / ')}` : ''}</span>{s.page ? <time>стр. {s.page}</time> : null}</div>
              <div className="nm-micro nm-muted">ID: {s.section_id || '—'} · источник метки: {s.source || '—'} · содержательный: {s.is_meaningful === false ? 'нет' : 'да'}</div>
              {s.is_meaningful === false ? <div className="nm-small nm-muted">Служебный фрагмент (не размечается): {s.reject_reason || ''}</div> : (
                <>
                  {s.why && <div className="nm-context"><b>Почему так размечено:</b> {s.why}</div>}
                  <div className="nm-small"><b>Этапы: </b>{(s.stages || []).length ? s.stages!.map((st, j) => <Badge key={j} tone="accent">{st.title || st.id}</Badge>) : '—'}</div>
                  <div className="nm-small"><b>Подэтапы: </b>{(s.substages || []).length ? s.substages!.map((su, j) => (
                    <Badge key={j} tone={su.confidence != null && su.confidence >= 0.6 ? 'ok' : 'warn'} title={su.description}>{su.title || su.id}{su.confidence != null ? ` · ${Math.round(su.confidence * 100)}%` : ''}</Badge>)) : '—'}</div>
                  <div className="nm-small"><b>Профессии: </b>{s.is_general ? <Badge tone="ok">общий для всех</Badge> : (s.professions || []).length
                    ? <>{s.professions!.map((p) => <Badge key={p} tone="muted">{p}</Badge>)}{s.prof_conf != null && <span className="nm-muted"> · {Math.round(s.prof_conf * 100)}%</span>}</> : '—'}</div>
                </>
              )}
              {s.chunks?.some((ch) => ch.meta) ? s.chunks.map((ch) => <Fragment key={ch.seq} ch={ch} n={ch.seq + 1} />)
                : <div className="nm-chunk"><div className="nm-pre">{s.text || ''}</div></div>}
            </Card>
          ))}
        </>
      )}
    </div>
  );
}

function Fragment({ ch, n }: { ch: Chunk; n: number }) {
  const m = ch.meta || {};
  const votes = Object.entries(m.votes || {}).filter(([, v]) => v).map(([k]) => VOTE[k] || k);
  const title = (id: string) => (m.candidates || []).find((c) => c.id === id)?.title || id;
  return (
    <div className="nm-chunk">
      <div className="nm-micro nm-muted">
        Фрагмент {n}{n > 1 ? ` · граница: ${votes.join(', ') || '—'}${m.boundary_score != null ? ` (вес ${m.boundary_score})` : ''}` : ''}
        {m.picked_by ? ` · итог выбрал: ${PICKED[m.picked_by] || m.picked_by}` : ''}
      </div>
      {!!m.keywords?.length && <div className="nm-small"><b>Ключевые слова: </b>{m.keywords.map(([w]) => w).join(', ')}</div>}
      {!!m.candidates?.length && <div className="nm-small"><b>Кандидаты: </b>{m.candidates.map((c) => `${c.title} — ${c.score}`).join('; ')}</div>}
      <div className="nm-small"><b>Итог: </b>{ch.is_general ? <Badge tone="ok">общая информация</Badge> : (ch.substages || []).length
        ? ch.substages!.map((su) => <Badge key={su.id} tone="accent">{title(su.id)}</Badge>) : '—'}</div>
      <div className="nm-pre">{ch.text}</div>
    </div>
  );
}
