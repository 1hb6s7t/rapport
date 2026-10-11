import { useEffect, useState } from "react";
import { Archive, FolderSearch, Layers3 } from "lucide-react";
import { api } from "@/api/client";
import { WorkbenchDatasetExport } from "@/components/workbenchDatasetExport";
import { Alert, Badge, Button, Card, EmptyState, ErrorBox, Field, Modal, Spinner, StatTile } from "@/components/ui";
import { formatDate, useWorkbenchAction } from "@/lib/workbench";

export type DatasetPool = "offline_review" | "realtime_review" | "personal_reply";
export interface DatasetManifest {
  id: string;
  status: string;
  created_at: string;
  version?: string;
  counts: Record<string, number>;
  quality?: Record<string, unknown>;
  pools?: Partial<Record<DatasetPool, number>>;
  eligible?: Partial<Record<DatasetPool, number>>;
  human_reviewed?: Partial<Record<DatasetPool, number>>;
  splits?: Partial<Record<DatasetPool, Record<string, number>>>;
  provenance?: { kind: string; source_id: string; sha256: string; version?: string }[];
  warnings?: string[];
}
interface DatasetPreview {
  items: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
  pool: DatasetPool;
}
const POOLS: { id: DatasetPool; label: string; hint: string }[] = [
  { id: "offline_review", label: "历史复盘", hint: "可参考后续消息；不代表当时就能得出的判断。" },
  { id: "realtime_review", label: "实时判断待复核", hint: "仅保留当时已出现的上下文；AI 辅助标签仍需核实后才能用于实时模型。" },
  { id: "personal_reply", label: "本人的表达", hint: "以前面的对话为输入，以本人真实发出的回复为目标。" },
];
function compactValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(compactValue).filter(Boolean).join("、");
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    if (typeof item.text === "string") return item.text;
    if (typeof item.content === "string") return item.content;
  }
  return "";
}
function SampleCard({ item, index, onReview, disabled = false }: { item: Record<string, unknown>; index: number; onReview?: () => void; disabled?: boolean }) {
  const context = item.context ?? item.messages ?? [];
  const content = compactValue(item.response ?? item.target ?? item.text ?? item.reply ?? item.target_text);
  return <article className="rounded-2xl border border-line bg-paper/70 p-4">
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-3">
      <span>样本 {index + 1}</span>
      {typeof item.split === "string" && <Badge>{{ train: "训练", validation: "验证", test: "测试" }[item.split] ?? item.split}</Badge>}
      {(item.needs_review === true || item.requires_review === true) && <Badge tone="warn">待复核</Badge>}
      {(item.post_hoc === true || item.uses_future_evidence === true) && <Badge tone="lavender">含后续证据</Badge>}
      {item.human_confirmed === true && <Badge tone="mint">已人工复核</Badge>}
      {item.training_eligible === false && <Badge>暂不用于训练</Badge>}
      {item.media_unknown === true && <Badge tone="warn">媒体内容未取得</Badge>}
    </div>
    {Array.isArray(context) && context.length > 0 && <div className="max-h-52 space-y-2 overflow-auto rounded-xl bg-card/60 p-3 text-sm">
      {context.map((line, i) => {
        const obj = line && typeof line === "object" ? line as Record<string, unknown> : {};
        const sender = obj.sender ?? obj.role;
        return <p key={i} className="whitespace-pre-wrap break-words"><span className="mr-2 text-xs text-ink-3">{sender === "me" || sender === "assistant" ? "我" : sender === "other" || sender === "user" ? "对方" : "消息"}</span>{compactValue(line)}</p>;
      })}
    </div>}
    {content && <p className="mt-3 whitespace-pre-wrap break-words text-sm text-ink">{content}</p>}
    {item.labels != null && typeof item.labels === "object" && <div className="mt-3 flex flex-wrap gap-2">{Object.entries(item.labels as Record<string, unknown>).map(([label, value]) => <Badge key={label} tone="lavender">{label === "emotion" ? "情绪" : label === "intent" ? "意图" : label}：{compactValue(value) || "未标注"}</Badge>)}</div>}
    {Array.isArray(item.evidence) && item.evidence.length > 0 && <details className="mt-3 text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看判断引用的证据</summary><ul className="mt-2 space-y-2">{item.evidence.map((evidence, i) => <li key={i} className="whitespace-pre-wrap break-words rounded-xl border-l-2 border-brand-soft bg-card/50 p-2">{compactValue(evidence) || "非文字证据，需另行核对"}</li>)}</ul></details>}
    {onReview && <Button size="sm" className="mt-3" disabled={disabled} onClick={onReview}>复核这条样本</Button>}
    <details className="mt-3 text-xs text-ink-3">
      <summary className="cursor-pointer text-brand-ink">查看标签与来源记录</summary>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-paper p-3">{JSON.stringify(item, null, 2)}</pre>
    </details>
  </article>;
}

export function WorkbenchDataset() {
  const [sourceRoot, setSourceRoot] = useState("");
  const [items, setItems] = useState<DatasetManifest[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [pool, setPool] = useState<DatasetPool>("offline_review");
  const [page, setPage] = useState(0);
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [selectedSamples, setSelectedSamples] = useState<string[]>([]);
  const [review, setReview] = useState<Record<string, unknown> | null>(null);
  const [reviewNote, setReviewNote] = useState("");
  const { busy, error, setError, run } = useWorkbenchAction();
  const current = items.find(item => item.id === selected);
  useEffect(() => {
    let alive = true;
    api.get<{ items: DatasetManifest[]; default_source?: string | null }>("/datasets").then(data => {
      if (alive) {
        setItems(data.items);
        setSelected(data.items[0]?.id ?? "");
        setSourceRoot(current => current || data.default_source || "");
      }
    }).catch(err => { if (alive) setError(err); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [setError]);
  useEffect(() => {
    let alive = true;
    setPreview(null);
    if (!selected || current?.status !== "built") { setPreviewLoading(false); return; }
    setPreviewLoading(true);
    api.get<DatasetPreview>(`/datasets/${encodeURIComponent(selected)}/preview?pool=${pool}&page=${page}&page_size=10`).then(data => {
      if (alive) setPreview(data);
    }).catch(err => { if (alive) setError(err); }).finally(() => { if (alive) setPreviewLoading(false); });
    return () => { alive = false; };
  }, [selected, current?.status, pool, page, setError]);
  function receive(manifest: DatasetManifest, keepPage = false) {
    setItems(old => [manifest, ...old.filter(item => item.id !== manifest.id)]);
    setSelected(manifest.id);
    if (!keepPage) setPage(0);
    setSelectedSamples([]);
  }
  return <div className="space-y-5">
    <Card title="让已有资料成为可靠的起点" hint="原始目录保持只读；检查、分组和导出都生成独立副本。" right={<FolderSearch size={22} className="text-brand" />}>
      <form onSubmit={event => { event.preventDefault(); void run("正在检查本地资料…", async () => receive(await api.post<DatasetManifest>("/datasets/audit", { source_root: sourceRoot.trim() }))); }} className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1"><Field label="已脱敏、已标注的本地文件夹"><input className="input" value={sourceRoot} onChange={event => setSourceRoot(event.target.value)} autoComplete="off" spellCheck={false} disabled={!!busy} /></Field></div>
        <Button variant="primary" type="submit" disabled={!!busy || !sourceRoot.trim()}><FolderSearch size={16} />检查资料</Button>
      </form>
      <p className="mt-3 text-xs leading-relaxed text-ink-3">检查当前标注、证据位置、重复消息与来源版本。自动质量分层不等于人工确认；涉及后文的判断会与实时样本分开。</p>
    </Card>
    <ErrorBox error={error} />
    {busy && <Spinner label={busy} />}
    {loading ? <Spinner label="读取资料记录…" /> : items.length === 0 ? <Card><EmptyState title="从你的现有资料开始" hint="检查文件夹后，这里会显示真实数量、待复核项和来源记录。" /></Card> : <>
      <Field label="资料版本"><select className="input" value={selected} onChange={event => { setSelected(event.target.value); setPage(0); setSelectedSamples([]); }} disabled={!!busy}>
        {items.map(item => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {item.status === "built" ? "已整理" : "已检查"} · {item.id.slice(0, 8)}</option>)}
      </select></Field>
      {current && <>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="消息" value={(current.counts.messages ?? 0).toLocaleString()} sub={`${current.counts.sessions ?? 0} 组聊天`} />
          <StatTile label="标注" value={(current.counts.annotations ?? 0).toLocaleString()} sub={`${current.counts.annotation_batches ?? 0} 个批次`} />
          <StatTile label="标记待复核" value={(current.counts.needs_review ?? 0).toLocaleString()} sub="不会视为人工金标准" />
          <StatTile label="引用后续证据" value={(current.counts.future_evidence ?? 0).toLocaleString()} sub="与实时任务隔离" />
        </div>
        <Card title="分成适合不同任务的样本" hint="保留完整会话的归属与时间顺序，避免重复上下文进入不同集合。">
          <div className="grid gap-3 md:grid-cols-3">{POOLS.map(option => <div key={option.id} className="rounded-2xl border border-line bg-paper/70 p-4"><h3 className="text-sm font-medium">{option.label}</h3><p className="my-2 text-2xl tabular-nums text-brand-ink">{current.pools?.[option.id]?.toLocaleString() ?? "—"}</p><p className="text-xs leading-relaxed text-ink-3">{option.hint}</p>{current.eligible?.[option.id] != null && <p className="mt-2 text-xs text-brand-ink">符合训练条件 {current.eligible[option.id]?.toLocaleString()} 条 · 已人工复核 {current.human_reviewed?.[option.id] ?? 0} 条</p>}{current.splits?.[option.id] && <p className="mt-3 text-xs text-ink-3">训练 {current.splits[option.id]?.train ?? 0} · 验证 {current.splits[option.id]?.validation ?? 0} · 测试 {current.splits[option.id]?.test ?? 0}</p>}</div>)}</div>
          {(current.warnings ?? []).length > 0 && <div className="mt-4"><Alert tone="warn"><ul className="list-disc space-y-1 pl-5">{current.warnings?.map((warning, i) => <li key={i}>{warning}</li>)}</ul></Alert></div>}
          <div className="mt-4 flex flex-wrap gap-2"><Button variant="primary" disabled={!!busy} onClick={() => void run("正在整理样本…", async () => receive(await api.post<DatasetManifest>("/datasets/build", { audit_id: current.id })))}><Layers3 size={16} />{current.status === "built" ? "重新整理版本" : "整理样本"}</Button>
            <WorkbenchDatasetExport key={current.id} datasetId={current.id} defaultPool={pool} selectedSampleIds={selectedSamples} disabled={!!busy || current.status !== "built"} />
          </div>
          <details className="mt-4 text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看来源与版本</summary><div className="mt-2 max-h-64 space-y-2 overflow-auto">{current.provenance?.map((source, i) => <div key={i} className="rounded-xl bg-paper p-3"><p>{source.kind} · {source.version ?? "未注明版本"}</p><p className="mt-1 break-all">匿名来源 {source.source_id}</p><p className="mt-1 break-all font-mono text-[10px]">{source.sha256}</p></div>)}</div></details>
        </Card>
        {current.status === "built" && <Card title="抽看真实样本" hint="以下内容只在本页临时显示，不写入浏览器存储。" right={<Archive size={20} className="text-lavender-deep" />}>
          <Field label="样本用途"><select className="input" value={pool} onChange={event => { setPool(event.target.value as DatasetPool); setPage(0); setSelectedSamples([]); }}>{POOLS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></Field>
          <p className="mb-4 mt-2 text-xs text-ink-3">{POOLS.find(option => option.id === pool)?.hint}</p>{selectedSamples.length > 0 && <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-ink-3"><span>已选 {selectedSamples.length} 条，翻页后仍保留选择。</span><Button size="sm" onClick={() => setSelectedSamples([])}>清除选择</Button><WorkbenchDatasetExport datasetId={current.id} defaultPool={pool} selectedSampleIds={selectedSamples} disabled={!!busy} /></div>}
          {previewLoading ? <Spinner label="读取样本…" /> : preview && preview.items.length > 0 ? <><div className="space-y-3">{preview.items.map((item, i) => <div key={`${selected}-${pool}-${page}-${i}`} className="space-y-2"><label className="flex items-center gap-2 text-xs text-ink-3"><input type="checkbox" disabled={!!busy} checked={selectedSamples.includes(String(item.id))} onChange={event => setSelectedSamples(old => event.target.checked ? [...old, String(item.id)] : old.filter(id => id !== String(item.id)))} />选择用于导出的样本 {page * preview.page_size + i + 1}</label><SampleCard item={item} index={page * preview.page_size + i} disabled={!!busy} onReview={() => { setReview(item); setReviewNote(""); }} /></div>)}</div><div className="mt-4 flex flex-wrap items-center justify-between gap-3"><span className="text-xs text-ink-3">共 {preview.total.toLocaleString()} 条 · 第 {page + 1} 页</span><div className="flex gap-2"><Button size="sm" disabled={page === 0} onClick={() => setPage(value => value - 1)}>上一页</Button><Button size="sm" disabled={(page + 1) * preview.page_size >= preview.total} onClick={() => setPage(value => value + 1)}>下一页</Button></div></div></> : <EmptyState title="这一组暂无合格样本" hint="可以查看其他分组或检查质量报告。" />}
        </Card>}
      </>}
    </>}
    <Modal open={!!review} onClose={() => { if (!busy) setReview(null); }} title="复核上下文与标签" width={720}>
      {review && <div className="space-y-4"><Alert tone="info">{pool === "realtime_review" ? "请只依据下面已出现的聊天，判断标签是否得到支持。确认后，这条样本才可用于实时模型训练。" : pool === "personal_reply" ? "请核对这是你实际发出的回复，上下文是否完整。如果内容缺失或不适合学习，可将它排除。" : "请核对原文、标签和证据。历史复盘可以参考后文，但不会因此变为可用的实时训练样本。"}</Alert><SampleCard item={review} index={0} /><Field label="复核备注（可不填）"><textarea className="input min-h-20" value={reviewNote} maxLength={1000} onChange={event => setReviewNote(event.target.value)} disabled={!!busy} /></Field><ErrorBox error={error} /><div className="flex flex-wrap justify-end gap-2"><Button disabled={!!busy} onClick={() => setReview(null)}>稍后复核</Button><Button disabled={!!busy} onClick={() => void run("正在保存复核结果…", async () => { receive(await api.post<DatasetManifest>(`/datasets/${encodeURIComponent(selected)}/reviews`, { sample_id: review.id, pool, decision: "excluded", note: reviewNote }), true); setReview(null); })}>排除这条</Button><Button variant="primary" disabled={!!busy || review.media_unknown === true} onClick={() => void run("正在保存复核结果…", async () => { receive(await api.post<DatasetManifest>(`/datasets/${encodeURIComponent(selected)}/reviews`, { sample_id: review.id, pool, decision: "accepted", note: reviewNote }), true); setReview(null); })}>确认标注可靠</Button></div></div>}
    </Modal>
  </div>;
}
