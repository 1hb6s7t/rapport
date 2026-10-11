import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { api } from "@/api/client";
import { Alert, Badge, Button, EmptyState, ErrorBox, Field, Modal, Spinner } from "@/components/ui";
import { downloadWorkbench, formatDate, useWorkbenchAction } from "@/lib/workbench";
import type { DatasetPool } from "@/components/workbenchDataset";

type Choice = { id: string; label?: string; session_id?: string; counts?: Record<string, number> };
interface Options { sessions: Choice[]; friends: Choice[]; conversations: Choice[]; splits: string[] }
interface Preview {
  token: string; expires_at: string; items: Record<string, unknown>[]; total: number; page: number; page_size: number;
  target_environment: string; selectors: Record<string, unknown>;
  manifest: { dataset_id: string; counts: Record<string, number>; files: string[]; warnings?: string[]; selectors?: Record<string, unknown>; target_environment?: string };
}
const POOL_NAMES: Record<DatasetPool, string> = { offline_review: "历史复盘", realtime_review: "实时判断", personal_reply: "本人的表达" };
const SPLIT_NAMES: Record<string, string> = { train: "训练", validation: "验证", test: "测试" };
function toggle(items: string[], id: string) { return items.includes(id) ? items.filter(item => item !== id) : [...items, id]; }

function ChoiceList({ title, items, selected, change }: { title: string; items: Choice[]; selected: string[]; change: (value: string[]) => void }) {
  const [search, setSearch] = useState("");
  const [shown, setShown] = useState(60);
  const filtered = search ? items.filter((item, index) => `${item.label ?? `${title} ${index + 1}`} ${item.id}`.includes(search)) : items;
  if (!items.length) return <p className="text-xs text-ink-3">{title}：没有可选择的记录。</p>;
  return <details className="rounded-xl border border-line p-3"><summary className="cursor-pointer text-sm">{title} · {selected.length ? `已选 ${selected.length} 项` : `全部 ${items.length} 项`}</summary><p className="mt-2 text-xs text-ink-3">不勾选表示不限此条件。不同类别的筛选条件同时生效。</p>{items.length > 20 && <input className="input mt-2" value={search} aria-label={`查找${title}`} placeholder={`查找${title}编号`} onChange={event => { setSearch(event.target.value); setShown(60); }} />}<div className="mt-2 max-h-48 space-y-2 overflow-auto">{filtered.slice(0, shown).map((item, index) => <label key={item.id} className="flex items-center justify-between gap-2 text-xs"><span className="flex items-center gap-2"><input type="checkbox" checked={selected.includes(item.id)} onChange={() => change(toggle(selected, item.id))} />{item.label ?? `${title} ${index + 1}`}</span><span className="text-ink-3">{Object.values(item.counts ?? {}).reduce((sum, value) => sum + value, 0)} 条</span></label>)}</div>{filtered.length > shown && <Button size="sm" className="mt-2" onClick={() => setShown(value => value + 60)}>查看更多</Button>}{selected.length > 0 && <Button size="sm" className="mt-2" onClick={() => change([])}>清除此筛选</Button>}</details>;
}

export function WorkbenchDatasetExport({ datasetId, defaultPool, selectedSampleIds = [], disabled }: { datasetId: string; defaultPool: DatasetPool; selectedSampleIds?: string[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<Options | null>(null);
  const [pools, setPools] = useState<string[]>([defaultPool]);
  const [splits, setSplits] = useState<string[]>(["train"]);
  const [sessions, setSessions] = useState<string[]>([]), [friends, setFriends] = useState<string[]>([]), [conversations, setConversations] = useState<string[]>([]);
  const [eligibleOnly, setEligibleOnly] = useState(true), [useSelected, setUseSelected] = useState(false);
  const [maximum, setMaximum] = useState("1000");
  const [target, setTarget] = useState("local"), [externalTarget, setExternalTarget] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false), [done, setDone] = useState(false);
  const { busy, error, run } = useWorkbenchAction();
  const environment = target === "local" ? "local" : externalTarget.trim();
  const validMaximum = maximum === "" || (Number.isInteger(Number(maximum)) && Number(maximum) >= 1 && Number(maximum) <= 100000);
  useEffect(() => { setOpen(false); setPreview(null); setDone(false); }, [datasetId]);
  async function start() {
    setOpen(true); setPreview(null); setOptions(null); setDone(false); setConfirmed(false); setPools([defaultPool]);
    setUseSelected(false); setSessions([]); setFriends([]); setConversations([]);
    await run("读取可选范围…", async () => setOptions(await api.get<Options>(`/datasets/${encodeURIComponent(datasetId)}/export-options`)));
  }
  const canPreview = !!options && pools.length > 0 && splits.length > 0 && validMaximum && !!environment && (!useSelected || selectedSampleIds.length > 0);
  async function makePreview() {
    await run("准备精确的导出预览…", async () => {
      const result = await api.post<Preview>(`/datasets/${encodeURIComponent(datasetId)}/export-preview`, {
        pools, splits, session_ids: sessions, friend_ids: friends, conversation_ids: conversations,
        sample_ids: useSelected ? selectedSampleIds : [], eligible_only: eligibleOnly,
        max_samples: maximum ? Number(maximum) : null, target_environment: environment,
      });
      setPreview(result); setConfirmed(false);
    });
  }
  async function turnPage(page: number) {
    if (!preview) return;
    await run("读取导出样本…", async () => {
      const result = await api.post<Preview>(`/datasets/${encodeURIComponent(datasetId)}/export-preview/page`, { token: preview.token, page, page_size: preview.page_size });
      setPreview(old => old ? { ...old, ...result } : null);
    });
  }
  return <>
    <Button disabled={disabled || !!busy} onClick={() => void start()}><Download size={16} />选择并预览导出</Button>
    {done && <Badge tone="mint">已生成本地下载</Badge>}
    <Modal open={open} onClose={() => { if (!busy) setOpen(false); }} title={preview ? "核对即将下载的数据包" : "选择数据包包含的内容"} width={850}>
      <div className="space-y-4"><ErrorBox error={error} />{busy && <Spinner label={busy} />}
        {!preview ? <>
          <p className="text-sm text-ink-2">先选择用途、资料范围和数量，再逐页核对实际导出内容。这里只生成下载包，不会连接外部训练环境。</p>
          {!options && !busy && <Button onClick={() => void start()}>重新读取范围</Button>}
          {options && <fieldset disabled={!!busy} className="space-y-4">
            <fieldset><legend className="mb-2 text-sm font-medium">用途</legend><div className="flex flex-wrap gap-4">{Object.entries(POOL_NAMES).map(([id, label]) => <label className="flex items-center gap-2 text-sm" key={id}><input type="checkbox" checked={pools.includes(id)} onChange={() => setPools(toggle(pools, id))} />{label}</label>)}</div></fieldset>
            <fieldset><legend className="mb-2 text-sm font-medium">数据划分</legend><div className="flex flex-wrap gap-4">{options.splits.map(id => <label className="flex items-center gap-2 text-sm" key={id}><input type="checkbox" checked={splits.includes(id)} onChange={() => setSplits(toggle(splits, id))} />{SPLIT_NAMES[id] ?? id}</label>)}</div><p className="mt-2 text-xs text-ink-3">默认只导出训练分区。验证和测试资料应独立保留，不混入训练。</p></fieldset>
            <div className="grid gap-3 sm:grid-cols-2"><ChoiceList title="对象" items={options.friends} selected={friends} change={setFriends} /><ChoiceList title="聊天记录" items={options.sessions} selected={sessions} change={setSessions} /></div>
            <ChoiceList title="会话段" items={options.conversations} selected={conversations} change={setConversations} />
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={eligibleOnly} onChange={event => setEligibleOnly(event.target.checked)} />只包含符合训练条件的样本</label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={useSelected} disabled={!selectedSampleIds.length} onChange={event => setUseSelected(event.target.checked)} />只包含页面中已勾选的 {selectedSampleIds.length} 条样本</label>
            <Field label="最多导出多少条" hint="留空表示所选范围的全部样本。"><input className="input" type="number" min={1} max={100000} value={maximum} onChange={event => setMaximum(event.target.value)} /></Field>
            <Field label="准备在哪里使用"><select className="input" value={target} onChange={event => setTarget(event.target.value)}><option value="local">仅在本机使用</option><option value="external">准备给指定外部环境使用</option></select></Field>
            {target === "external" && <Field label="目标环境名称" hint="只填写服务或运行环境名称，不要填写 API Key、密钥或私密地址。"><input className="input" value={externalTarget} maxLength={200} onChange={event => setExternalTarget(event.target.value)} placeholder="例如：我的云端训练实例" autoComplete="off" /></Field>}
          </fieldset>}
          {!eligibleOnly && <Alert tone="warn">包含待复核样本时，请将质量状态随数据一起保留，不能把它们直接当作正确答案训练。</Alert>}
          <div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setOpen(false)}>取消</Button><Button variant="primary" disabled={!!busy || !canPreview} onClick={() => void makePreview()}>生成精确预览</Button></div>
        </> : <>
          <Alert tone="info">这份预览已固定所选范围、版本、顺序和目标环境。下载前改选内容，需要重新预览。</Alert>
          <div className="grid gap-3 rounded-xl bg-paper p-3 text-sm sm:grid-cols-2"><p>包含样本：{preview.total.toLocaleString()} 条</p><p>目标：{preview.target_environment === "local" ? "本机" : preview.target_environment}</p>{Object.entries(preview.manifest.counts).map(([id, count]) => <p key={id}>{POOL_NAMES[id as DatasetPool] ?? id}：{count.toLocaleString()} 条</p>)}</div>
          {preview.manifest.warnings?.map((warning, index) => <Alert key={index} tone="warn">{warning}</Alert>)}
          {preview.items.length === 0 ? <EmptyState title="这个范围没有可导出的样本" hint="请返回调整用途、分区或质量筛选。" /> : <div className="space-y-3">{preview.items.map((item, index) => <details key={String(item.id ?? index)} className="rounded-xl border border-line p-3 text-sm"><summary className="cursor-pointer">第 {preview.page * preview.page_size + index + 1} 条 · {SPLIT_NAMES[String(item.split)] ?? "样本"}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(item, null, 2)}</pre></details>)}</div>}
          <div className="flex items-center justify-between gap-2"><Button size="sm" disabled={!!busy || preview.page === 0} onClick={() => void turnPage(preview.page - 1)}>上一页</Button><span className="text-xs text-ink-3">第 {preview.page + 1} 页 / 共 {Math.max(1, Math.ceil(preview.total / preview.page_size))} 页</span><Button size="sm" disabled={!!busy || (preview.page + 1) * preview.page_size >= preview.total} onClick={() => void turnPage(preview.page + 1)}>下一页</Button></div>
          <details className="text-xs text-ink-3"><summary className="cursor-pointer">导出清单和选择范围</summary><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ files: preview.manifest.files, selectors: preview.selectors ?? preview.manifest.selectors }, null, 2)}</pre></details>
          <p className="text-xs text-ink-3">预览有效至 {formatDate(preview.expires_at)}。文件包含所选原文；匿名标识不会自动消除文字中尚存的身份线索，请核对内容。</p>
          <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={!!busy} />我已核对范围、内容和目标环境，确认生成这份下载包。</label>
          <div className="flex flex-wrap justify-end gap-2"><Button disabled={!!busy} onClick={() => { setPreview(null); setConfirmed(false); }}>返回调整</Button><Button variant="primary" disabled={!!busy || !confirmed || preview.total === 0} onClick={() => void run("生成本地下载包…", async () => { await downloadWorkbench(`/datasets/${encodeURIComponent(datasetId)}/export`, { token: preview.token, confirmed: true, target_environment: preview.target_environment }, "rapport-selected-dataset.zip"); setDone(true); setOpen(false); })}>确认下载，不自动上传</Button></div>
        </>}
      </div>
    </Modal>
  </>;
}
