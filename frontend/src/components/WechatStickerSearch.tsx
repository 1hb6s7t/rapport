import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { Alert, Badge, Button, Card, ErrorBox, Modal } from "@/components/ui";

type Choice = { token: string; index: number; label: string; needs_ocr: boolean; sha256: string; preview_url: string };
type Search = { contact: string; query: string; candidates: Choice[]; expires_in_s: number; note: string };
type StickerItem = { kind: "wechat_sticker"; query: string; label: string; sha256: string; preview_url: string; ocr_text?: string | null; ocr_used: boolean };
type Receipt = { token: string; digest: string; contact: string; status: string; items: StickerItem[];
  results: { index: number; status: "pending" | "sent" | "failed" | "uncertain"; code?: string | null }[] };

/** A WeChat search result is only ever sent after a second, concrete preview. */
export function WechatStickerSearch({ captureToken, relationshipId, importId, enabled }: {
  captureToken: string; relationshipId: string; importId: string; enabled: boolean;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Search | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const scope = JSON.stringify({ captureToken, relationshipId, importId });
  const current = useRef(scope); current.current = scope;
  const currentQuery = useRef(query); currentQuery.current = query;
  useEffect(() => { setResults(null); setReceipt(null); setError(null); }, [scope]);

  async function search() {
    const started = scope;
    const searched = query.trim();
    setBusy(true); setError(null); setResults(null); setReceipt(null);
    try {
      const value = await api.post<Search>("/dispatch/stickers/search", {
        capture_token: captureToken, relationship_id: relationshipId, import_id: importId, query: searched,
      });
      if (started === current.current && currentQuery.current.trim() === searched) setResults(value);
    } catch (value) { if (started === current.current) setError(value); }
    finally { setBusy(false); }
  }

  async function preview(choice: Choice) {
    const started = scope;
    const searched = results?.query;
    setBusy(true); setError(null); setReceipt(null);
    try {
      const value = await api.post<Receipt>("/dispatch/stickers/preview", {
        capture_token: captureToken, relationship_id: relationshipId, import_id: importId,
        selection_token: choice.token,
      });
      if (started === current.current && currentQuery.current.trim() === searched) setReceipt(value);
    } catch (value) { if (started === current.current) setError(value); }
    finally { setBusy(false); }
  }

  async function confirm() {
    if (!receipt || receipt.status !== "preview") return;
    const started = scope;
    const prior = receipt;
    setBusy(true); setError(null);
    // An uncertain network result must never offer a second send button.
    setReceipt({ ...prior, status: "checking" });
    try {
      const value = await api.post<Receipt>("/dispatch/stickers/confirm", { token: prior.token, digest: prior.digest });
      if (started === current.current) setReceipt(value);
    } catch (value) {
      if (started === current.current) { setError(value); setReceipt({ ...prior, status: "unknown" }); }
    } finally { setBusy(false); }
  }

  async function refresh() {
    if (!receipt) return;
    const started = scope;
    setBusy(true); setError(null);
    try {
      const value = await api.post<Receipt>("/dispatch/status", { token: receipt.token });
      if (started === current.current) setReceipt(value);
    } catch (value) { if (started === current.current) setError(value); }
    finally { setBusy(false); }
  }

  const item = receipt?.items[0];
  const result = receipt?.results[0];
  return <Card title="从微信搜索表情" hint="输入关键词，先看具体表情，再决定是否发送。能读取无障碍名称时不启动 OCR；名称不清楚时只识别你选中的小图。">
    <div className="flex flex-wrap gap-2">
      <input className="input min-w-48 flex-1" aria-label="微信表情搜索词" value={query} maxLength={24}
        onChange={event => { currentQuery.current = event.target.value; setQuery(event.target.value); setResults(null); setReceipt(null); setError(null); }}
        onKeyDown={event => { if (event.key === "Enter" && query.trim() && !busy) void search(); }}
        placeholder="例如：你好、晚安" />
      <Button disabled={busy || !query.trim()} onClick={search}>{busy && !results ? "正在搜索…" : "搜索微信表情"}</Button>
    </div>
    <p className="mt-2 text-xs text-ink-3">搜索在微信表情面板中执行。结果只展示当前可见缩略图；不会自动选图或发送。</p>
    <ErrorBox error={error} />
    {busy && results && !receipt && <p className="mt-2 text-sm" role="status">正在核对所选表情并准备预览…</p>}
    {results && <>
      <p className="mt-3 text-sm">{results.contact} · “{results.query}” · {results.candidates.length} 个可见结果</p>
      {results.candidates.length === 0 ? <Alert>没有可核对的可见结果，请换个搜索词。</Alert> :
        <div className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-5" role="list" aria-label="微信表情候选">
          {results.candidates.map(choice => <button key={choice.token} type="button" role="listitem"
            className="rounded-xl border border-[var(--border)] bg-white/70 p-2 text-left hover:border-[var(--accent)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
            disabled={busy} onClick={() => preview(choice)} title={choice.label || "待识别表情"}
            aria-label={`预览第${choice.index + 1}个表情：${choice.label || "未命名"}`}>
            <img src={choice.preview_url} alt={choice.label || "搜索结果表情"} className="aspect-square w-full rounded-lg object-contain" />
            <span className="mt-1 block truncate text-xs">{choice.label || "待识别表情"}</span>
            {choice.needs_ocr && <span className="text-xs text-ink-3">选中后识别文字</span>}
          </button>)}
        </div>}
    </>}
    <Modal open={Boolean(receipt)} onClose={() => { if (!busy) setReceipt(null); }} title={receipt ? `发送表情给 · ${receipt.contact}` : "预览表情"} width={520}>
      {receipt && item && <div className="space-y-3">
        <Alert>请核对收件人和这张具体表情。微信搜索结果会在发送前重新核验；变化或结果不确定时立即停止，不自动重发。</Alert>
        <img src={item.preview_url} alt={item.label || "待发送表情"} className="mx-auto max-h-64 max-w-full rounded-xl object-contain" />
        <p className="text-sm">搜索词：{item.query} · 微信名称：{item.label || "未命名"}</p>
        <p className="text-sm">{item.ocr_used ? `小图 OCR：${item.ocr_text || "没有可靠识别结果，请以图片为准"}` : "已直接读取微信表情名称，无需重复 OCR。"}</p>
        <p className="break-all text-xs text-ink-3">图片校验值：{item.sha256}</p>
        {receipt.status !== "preview" && <Badge tone={receipt.status === "complete" ? "good" : "warn"}>
          {receipt.status === "checking" ? "正在核对微信发送…" : receipt.status === "complete" ? "已核对发送" : `已停止或结果不确定：${result?.code || receipt.status}`}
        </Badge>}
        <ErrorBox error={error} />
        <div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setReceipt(null)}>关闭</Button>
          {receipt.status === "preview" ? <Button variant="primary" disabled={busy || !enabled} onClick={confirm}>确认发送这一张表情</Button>
            : <Button disabled={busy} onClick={refresh}>查询回执（不重发）</Button>}
        </div>
        {!enabled && <p className="text-xs text-ink-3">请先在微信读取页开启确认后输入权限。</p>}
      </div>}
    </Modal>
  </Card>;
}
