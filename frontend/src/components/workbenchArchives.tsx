import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Archive, Download, FileImage, Trash2, Upload } from "lucide-react";
import { api } from "@/api/client";
import { RelationshipPicker } from "@/components/RelationshipPicker";
import { Alert, Badge, Button, Card, EmptyState, ErrorBox, Field, Modal, Spinner, StatTile } from "@/components/ui";
import { downloadWorkbench, formatDate, useWorkbenchAction } from "@/lib/workbench";
import { useWechatTarget } from "@/lib/wechat";
import { useFlow } from "@/store";

interface ArchiveRecord {
  id: string;
  relationship_id: string;
  created_at: string;
  title?: string;
  message_count?: number;
  n_messages?: number;
  asset_count?: number;
  counts?: Record<string, number>;
}
interface ArchiveMessage { id?: string; message_ref: string; sender: string; text?: string; ts?: string; type: string; ts_precision?: string }
interface ArchiveAsset { id: string; message_ref: string; status: string; kind: string; filename?: string; mime?: string; size_bytes?: number }
interface MessagePage { items?: ArchiveMessage[]; messages?: ArchiveMessage[]; total: number; page: number; page_size: number }
interface ArchiveExport { token?: string; preview_token?: string; expires_at?: string; manifest?: Record<string, unknown>; [key: string]: unknown }
interface MediaCapturePreview { contact: string; message_ref: string; note: string; selections: { selection_token: string; preview: string; status: "thumbnail" }[] }
const MEDIA_STATUS: Record<string, string> = { original: "原件", thumbnail: "缩略图", missing: "缺少原件", unavailable: "无法下载", failed: "下载失败" };
const MESSAGE_KIND: Record<string, string> = { text: "文字", image: "图片", sticker: "表情", video: "视频", audio: "语音", voice: "语音", system: "系统消息" };
function messageTime(message: ArchiveMessage) {
  if (!message.ts || message.ts_precision === "none") return "时间未显示";
  const date = new Date(message.ts);
  if (Number.isNaN(date.getTime())) return message.ts;
  if (message.ts_precision === "day") return `${date.toLocaleDateString("zh-CN")} · 仅日期`;
  if (message.ts_precision === "label") return `${date.toLocaleString("zh-CN", { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })} · 按聊天时间标签`;
  if (message.ts_precision === "minute") return `${date.toLocaleString("zh-CN", { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })} · 精确到分钟`;
  return formatDate(message.ts);
}
function messageSequence(reference: string) { return Number(reference.split(":").at(-1)) + 1; }

export function WorkbenchArchives() {
  const [flow] = useFlow();
  return <div className="space-y-5"><Card title="按好友保留相处的记录"><RelationshipPicker label="档案所属对象" /></Card><ArchiveContents key={flow.relationshipId} relationshipId={flow.relationshipId} importId={flow.importId} /></div>;
}

function ArchiveContents({ relationshipId, importId }: { relationshipId: string; importId: string | null }) {
  const [items, setItems] = useState<ArchiveRecord[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [messages, setMessages] = useState<MessagePage | null>(null);
  const [assets, setAssets] = useState<ArchiveAsset[]>([]);
  const [page, setPage] = useState(0);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [remove, setRemove] = useState<ArchiveRecord | null>(null);
  const [exportPreview, setExportPreview] = useState<ArchiveExport | null>(null);
  const [includeMedia, setIncludeMedia] = useState(true);
  const [notice, setNotice] = useState("");
  const [uploadTo, setUploadTo] = useState<ArchiveAsset | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileStatus, setFileStatus] = useState<"original" | "thumbnail">("original");
  const [mediaLimit, setMediaLimit] = useState(24);
  const [collection, setCollection] = useState<{ asset: ArchiveAsset; data: MediaCapturePreview } | null>(null);
  const [selectionToken, setSelectionToken] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const { busy, error, setError, run } = useWorkbenchAction();
  const query = `relationship_id=${encodeURIComponent(relationshipId)}`;
  const current = items.find(item => item.id === selected);
  const wechat = useWechatTarget(importId ?? "", relationshipId);
  useEffect(() => {
    let alive = true;
    api.get<{ items?: ArchiveRecord[]; archives?: ArchiveRecord[] }>(`/archives?${query}`).then(data => {
      if (!alive) return;
      const records = data.items ?? data.archives ?? [];
      setItems(records);
      setSelected(old => records.some(item => item.id === old) ? old : records[0]?.id ?? "");
    }).catch(err => { if (alive) setError(err); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [query, refresh, setError]);
  useEffect(() => {
    let alive = true;
    setMessages(null); setAssets([]);
    if (!selected) { setReading(false); return; }
    setReading(true);
    Promise.all([
      api.get<MessagePage>(`/archives/${encodeURIComponent(selected)}?${query}&page=${page}&page_size=30`),
      api.get<{ items?: ArchiveAsset[]; assets?: ArchiveAsset[] }>(`/archives/${encodeURIComponent(selected)}/assets?${query}`),
    ]).then(([messagePage, media]) => { if (alive) { setMessages(messagePage); setAssets(media.items ?? media.assets ?? []); } }).catch(err => { if (alive) setError(err); }).finally(() => { if (alive) setReading(false); });
    return () => { alive = false; };
  }, [selected, query, page, refresh, setError]);
  return <div className="space-y-5">
    <Card title="保存为长期档案" hint="保留已确认的聊天副本，独立于临时导入的清理期限。" right={<Archive className="text-brand" size={22} />}>
      <div className="flex flex-wrap items-center gap-3"><Button variant="primary" disabled={!!busy || !importId} onClick={() => void run("正在保存长期档案…", async () => { const record = await api.post<ArchiveRecord>("/archives", { import_id: importId, relationship_id: relationshipId }); setSelected(record.id); setPage(0); setRefresh(value => value + 1); setNotice("已保留聊天副本，可继续补充对应的媒体原件。"); })}>保留当前已确认材料</Button><Link className="text-sm" to={importId ? "/review" : "/import"}>{importId ? "查看当前材料" : "先导入聊天"}</Link></div>
      <p className="mt-3 text-xs text-ink-3">图片、表情和视频的消息记录会保留；没有取得原文件时会明确显示缺失，不会用截图冒充原件。</p>
    </Card>
    <ErrorBox error={error} />{busy && <Spinner label={busy} />}{notice && <Alert tone="good">{notice}</Alert>}
    {loading ? <Spinner label="读取长期档案…" /> : items.length === 0 ? <Card><EmptyState title="这个对象还没有长期档案" hint="先导入并确认聊天，再保存一份长期副本。" /></Card> : <>
      <Field label="选择档案"><select className="input" value={selected} disabled={!!busy} onChange={event => { setSelected(event.target.value); setPage(0); setMediaLimit(24); setNotice(""); }}>{items.map(item => <option key={item.id} value={item.id}>{item.title || formatDate(item.created_at)} · {item.message_count ?? item.n_messages ?? item.counts?.messages ?? "—"} 条消息</option>)}</select></Field>
      {current && <Card title="档案内容" hint={`保存于 ${formatDate(current.created_at)}`} right={<Badge tone="mint">本地保存</Badge>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3"><StatTile label="消息" value={messages?.total ?? current.message_count ?? current.n_messages ?? "—"} /><StatTile label="媒体原件" value={assets.filter(asset => asset.status === "original").length} /><StatTile label="仍需补充" value={assets.filter(asset => asset.status !== "original").length} /></div>
        <div className="mt-4 flex flex-wrap items-center gap-3"><label className="flex items-center gap-2 text-sm text-ink-2"><input type="checkbox" checked={includeMedia} disabled={!!busy} onChange={event => setIncludeMedia(event.target.checked)} />导出时包含已有媒体</label><Button disabled={!!busy} onClick={() => void run("准备档案导出预览…", async () => setExportPreview(await api.post<ArchiveExport>(`/archives/${encodeURIComponent(selected)}/export-preview`, { relationship_id: relationshipId, include_media: includeMedia })))}><Download size={16} />预览导出</Button><Button variant="danger" disabled={!!busy} onClick={() => setRemove(current)}><Trash2 size={15} />删除档案</Button></div>
      </Card>}
      <Card title="聊天记录" hint="时间未显示或不精确时保留其原有精度。">
        {reading ? <Spinner label="读取这一页消息…" /> : messages && <><div id="workbench-archive-messages" className="max-h-[580px] space-y-3 overflow-auto p-1">{(messages.items ?? messages.messages ?? []).map(message => <article key={message.message_ref} className={`max-w-[90%] rounded-2xl border p-3 sm:max-w-[80%] ${message.sender === "me" ? "ml-auto border-brand-soft bg-brand-mist" : "mr-auto border-line bg-paper"}`}><p className="mb-1 text-[11px] text-ink-3">第 {messageSequence(message.message_ref)} 条 · {message.sender === "me" ? "我" : message.sender === "other" ? "对方" : "未知／系统"} · {messageTime(message)}</p><p className="whitespace-pre-wrap break-words text-sm">{message.text || `[${MESSAGE_KIND[message.type] ?? message.type}]`}</p></article>)}</div><div className="mt-4 flex items-center justify-between"><span className="text-xs text-ink-3">第 {page + 1} 页 · 共 {messages.total} 条</span><div className="flex gap-2"><Button size="sm" disabled={page === 0 || reading} onClick={() => setPage(value => value - 1)}>上一页</Button><Button size="sm" disabled={(page + 1) * messages.page_size >= messages.total || reading} onClick={() => setPage(value => value + 1)}>下一页</Button></div></div></>}
      </Card>
      {assets.length > 0 && <Card title="关联图片、表情和视频" hint="可以补充本地原文件，或从当前已绑定好友的微信窗口另存为。">
        {!wechat.target && <p className="mb-3 text-xs text-ink-3">需要从微信取得原件时，先在<Link to="/wechat">微信聊天读取</Link>中读取这个好友，选择“准备图片/表情回复”完成画面绑定。</p>}
        <div className="grid gap-3 sm:grid-cols-2">{assets.slice(0, mediaLimit).map(asset => <div key={asset.id} className="rounded-2xl border border-line bg-paper/70 p-4">
          <div className="flex items-center gap-2"><FileImage className="text-brand" size={18} /><span className="text-sm">{MESSAGE_KIND[asset.kind] ?? asset.kind}</span><Badge tone={asset.status === "original" ? "mint" : "warn"}>{MEDIA_STATUS[asset.status] ?? asset.status}</Badge></div>
          <p className="mt-2 text-xs text-ink-3">对应第 {messageSequence(asset.message_ref)} 条消息</p>
          {asset.filename && <p className="mt-1 truncate text-xs text-ink-3">{asset.filename}</p>}
          {asset.mime?.startsWith("image/") && asset.status !== "missing" && <img loading="lazy" className="mt-3 max-h-40 max-w-full rounded-xl object-contain" src={`/api/archives/assets/${encodeURIComponent(asset.id)}?${query}&inline=true`} alt={`第 ${messageSequence(asset.message_ref)} 条消息的${MESSAGE_KIND[asset.kind] ?? "媒体"}`} />}
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" disabled={!!busy || reading} onClick={() => { setPage(Math.floor((messageSequence(asset.message_ref) - 1) / 30)); document.getElementById("workbench-archive-messages")?.scrollIntoView({ behavior: "smooth", block: "center" }); }}>查看所在消息</Button>
            {(asset.status === "original" || asset.status === "thumbnail") && <a className="btn btn-ghost px-2.5 py-1 text-xs" href={`/api/archives/assets/${encodeURIComponent(asset.id)}?${query}`} download>下载已保存文件</a>}
            <Button size="sm" disabled={!!busy} onClick={() => { setUploadTo(asset); setFile(null); setFileStatus("original"); }}>补充文件</Button>
            {["image", "video", "sticker"].includes(asset.kind) && asset.status !== "original" && <Button size="sm" disabled={!!busy || !wechat.token || !wechat.target} onClick={() => void run("正在读取微信中的媒体预览…", async () => { const data = await api.post<MediaCapturePreview>("/dispatch/media/preview", { capture_token: wechat.token, relationship_id: relationshipId, archive_id: selected, message_ref: asset.message_ref }); setCollection({ asset, data }); setSelectionToken(""); })}>从微信另存为原件</Button>}
          </div>
        </div>)}</div>
        {assets.length > mediaLimit && <Button className="mt-4" onClick={() => setMediaLimit(value => value + 24)}>再看 24 个媒体记录（共 {assets.length} 个）</Button>}
      </Card>}
    </>}
    <Modal open={!!collection} onClose={() => { if (!busy) setCollection(null); }} title="核对微信中的原件" width={720}>
      {collection && <div className="space-y-4"><p className="text-sm text-ink-2">好友：<strong>{collection.data.contact}</strong> · 档案第 {messageSequence(collection.asset.message_ref)} 条消息</p><Alert tone="info">{collection.data.note}</Alert>{collection.data.selections.length === 0 ? <EmptyState title="当前画面没有可取得的媒体" hint="请在微信打开对应的图片、表情或视频，再重新读取预览。" /> : <fieldset><legend className="mb-2 text-sm font-medium">选择与这条归档消息一致的图片</legend><div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{collection.data.selections.map((selection, index) => <label key={selection.selection_token} className={`cursor-pointer rounded-2xl border p-3 ${selectionToken === selection.selection_token ? "border-brand bg-brand-mist" : "border-line bg-paper"}`}><input type="radio" name="wechat-media-selection" value={selection.selection_token} checked={selectionToken === selection.selection_token} disabled={!!busy} onChange={() => setSelectionToken(selection.selection_token)} className="mr-2" /><span className="text-xs">预览 {index + 1}</span><img className="mt-2 h-36 w-full rounded-lg object-contain" src={selection.preview} alt={`微信媒体预览 ${index + 1}`} /></label>)}</div></fieldset>}<ErrorBox error={error} /><div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setCollection(null)}>取消</Button><Button variant="primary" disabled={!!busy || !selectionToken} onClick={() => void run("正在通过微信另存为原件…", async () => { await api.post("/dispatch/media/collect", { capture_token: wechat.token, relationship_id: relationshipId, archive_id: selected, message_ref: collection.asset.message_ref, selection_token: selectionToken }); setCollection(null); setSelectionToken(""); setRefresh(value => value + 1); setNotice("已取得微信原件并加入长期档案。"); })}>确认保存此原件</Button></div></div>}
    </Modal>
    <Modal open={!!remove} onClose={() => { if (!busy) setRemove(null); }} title="删除这份长期档案？"><p className="text-sm text-ink-2">这会删除所选档案及其关联媒体副本，不能撤销。微信中的原聊天不会被删除。</p><ErrorBox error={error} /><div className="mt-4 flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setRemove(null)}>保留</Button><Button variant="danger" disabled={!!busy} onClick={() => void run("正在删除档案…", async () => { if (!remove) return; await api.del(`/archives/${encodeURIComponent(remove.id)}?${query}`); setRemove(null); setRefresh(value => value + 1); setNotice("所选长期档案已删除。"); })}>确认删除</Button></div></Modal>
    <Modal open={!!exportPreview} onClose={() => { if (!busy) setExportPreview(null); }} title="确认下载聊天档案"><div className="space-y-4"><Alert tone="warn">导出的压缩包包含可直接阅读的聊天{includeMedia ? "及媒体文件" : ""}，不再受本应用的本地加密保护。请保存在你信任的位置。</Alert><p className="text-sm text-ink-2">只生成本地下载，不会自动发送给外部服务。</p><details className="text-xs text-ink-3"><summary className="cursor-pointer">查看导出清单</summary><pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(exportPreview?.manifest ?? exportPreview, null, 2)}</pre></details><ErrorBox error={error} /><div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setExportPreview(null)}>取消</Button><Button variant="primary" disabled={!!busy} onClick={() => void run("正在生成档案包…", async () => { await downloadWorkbench(`/archives/${encodeURIComponent(selected)}/export`, { relationship_id: relationshipId, include_media: includeMedia, token: exportPreview?.token, confirmed: true }, "rapport-archive.zip"); setExportPreview(null); setNotice("已生成聊天档案下载。"); })}>确认下载</Button></div></div></Modal>
    <Modal open={!!uploadTo} onClose={() => { if (!busy) setUploadTo(null); }} title="补充这条消息的媒体文件"><form className="space-y-4" onSubmit={event => { event.preventDefault(); void run("正在保存媒体文件…", async () => { if (!file || !uploadTo) return; const data = new FormData(); data.set("file", file); data.set("relationship_id", relationshipId); data.set("message_ref", uploadTo.message_ref); data.set("status", fileStatus); data.set("kind", uploadTo.kind); await api.form(`/archives/${encodeURIComponent(selected)}/assets`, data); setUploadTo(null); setFile(null); if (inputRef.current) inputRef.current.value = ""; setRefresh(value => value + 1); setNotice("媒体文件已保存并关联。"); }); }}><Field label="选择本地文件"><input ref={inputRef} type="file" accept="image/*,video/*,audio/*" className="input" disabled={!!busy} onChange={event => setFile(event.target.files?.[0] ?? null)} required /></Field><Field label="这个文件是"><select className="input" value={fileStatus} disabled={!!busy} onChange={event => setFileStatus(event.target.value as "original" | "thumbnail")}><option value="original">完整原件</option><option value="thumbnail">只有缩略图</option></select></Field><p className="text-xs text-ink-3">请核对文件确实对应这条聊天消息；缩略图不会被记作原件。</p><ErrorBox error={error} /><div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setUploadTo(null)} type="button">取消</Button><Button type="submit" variant="primary" disabled={!!busy || !file}><Upload size={15} />保存文件</Button></div></form></Modal>
  </div>;
}
