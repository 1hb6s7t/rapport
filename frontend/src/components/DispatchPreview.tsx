import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { Alert, Badge, Button, ErrorBox, Modal } from "@/components/ui";

export type DispatchItem = { kind: "text" | "sticker" | "image"; text?: string | null; asset_id?: string | null };
type PreviewItem = DispatchItem & { preview_url?: string; caption?: string; sha256?: string; mime?: string };
type Receipt = { token: string; digest: string; contact: string; status: string; expires_in_s: number; items: PreviewItem[]; results: { index: number; status: "pending" | "sent" | "failed" | "uncertain"; code?: string | null }[] };
const resultLabels = { pending: "尚未发送", sent: "已核对发送", failed: "已停止", uncertain: "结果不确定，请检查微信" };

/** Preview is bound to exact recipient, content, ordering and immutable asset hashes. */
export function DispatchPreview({ captureToken, relationshipId, importId, items, disabled = false }: {
  captureToken: string; relationshipId: string; importId: string; items: DispatchItem[]; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<Receipt | null>(null), [error, setError] = useState<unknown>(null);
  const signature = JSON.stringify({ captureToken, relationshipId, importId, items });
  const current = useRef(signature); current.current = signature;
  useEffect(() => { setReceipt(null); setError(null); setOpen(false); }, [signature]);
  async function preview() {
    const started = signature;
    setBusy(true); setError(null); setOpen(true); setReceipt(null);
    try {
      const result = await api.post<Receipt>("/dispatch/preview", { capture_token: captureToken, relationship_id: relationshipId, import_id: importId, items });
      if (started === current.current) setReceipt(result);
    } catch (value) { if (started === current.current) setError(value); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!receipt || receipt.status !== "preview") return;
    const started = signature;
    setBusy(true); setError(null);
    // Disable confirmation immediately; a network error must not suggest re-sending.
    setReceipt({ ...receipt, status: "checking" });
    try {
      const result = await api.post<Receipt>("/dispatch/confirm", { token: receipt.token, digest: receipt.digest });
      if (started === current.current) setReceipt(result);
    } catch (value) {
      if (started === current.current) {
        setError(value);
        setReceipt({ ...receipt, status: "unknown" });
      }
    } finally { setBusy(false); }
  }
  async function refreshReceipt() {
    if (!receipt) return;
    const started = signature;
    setBusy(true); setError(null);
    try { const result = await api.post<Receipt>("/dispatch/status", { token: receipt.token }); if (started === current.current) setReceipt(result); }
    catch (value) { if (started === current.current) setError(value); } finally { setBusy(false); }
  }
  return <>
    <Button size="sm" disabled={disabled || busy || !items.length} onClick={preview}>预览整组发送</Button>
    <Modal open={open} onClose={() => { if (!busy) setOpen(false); }} title={receipt ? `发送给 · ${receipt.contact}` : "核对整组消息"} width={600}>
      <div className="space-y-4">
        <Alert>请核对收件人、内容和顺序。确认后会在微信逐项发送；对话变化或某一项结果不确定时立即停止，不自动重发。</Alert>
        <ErrorBox error={error} />
        {busy && !receipt && <p role="status">正在核对材料与素材…</p>}
        {receipt && <>
          <ol className="space-y-3" aria-label="发送顺序">
            {receipt.items.map((item, index) => <li key={`${index}-${item.asset_id ?? "text"}`} className="rounded-2xl border border-[var(--border)] bg-white/60 p-3">
              <div className="mb-2 flex items-center justify-between gap-2"><span className="text-sm">{index + 1}. {item.kind === "text" ? "文字" : item.kind === "sticker" ? "表情" : "图片"}</span>{receipt.status !== "preview" && <Badge>{resultLabels[receipt.results[index]?.status ?? "pending"]}</Badge>}</div>
              {item.kind === "text" ? <p className="whitespace-pre-wrap break-words">{item.text}</p> : <>
                {item.preview_url ? <img src={item.preview_url} alt={item.caption || "待发送的具体素材"} className="max-h-52 max-w-full rounded-xl object-contain" /> : <p>无法显示素材，暂不可确认发送。</p>}
                {item.caption && <p className="mt-1 text-sm">{item.caption}</p>}
              </>}
              {receipt.results[index]?.code && <p className="mt-2 text-sm text-[var(--muted)]">{receipt.results[index].code}</p>}
            </li>)}
          </ol>
          {receipt.status !== "preview" && receipt.status !== "complete" && <Alert>本组已停止或尚未确认完成。请查看微信中的实际结果，切勿直接重发整组。</Alert>}
          {receipt.status === "complete" && <Badge tone="good">整组已完成，逐项回执已保存</Badge>}
          <div className="flex flex-wrap justify-end gap-2"><Button disabled={busy} onClick={() => setOpen(false)}>关闭</Button>
            {receipt.status === "preview" ? <Button variant="primary" disabled={busy || receipt.items.some(item => item.kind !== "text" && !item.preview_url)} onClick={confirm}>确认发送整组</Button>
              : <Button disabled={busy} onClick={refreshReceipt}>查询回执（不重发）</Button>}
          </div>
        </>}
      </div>
    </Modal>
  </>;
}
