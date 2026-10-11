import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { Alert, Badge, Button, ErrorBox, Field, Modal } from "@/components/ui";
import type { WechatTarget } from "@/lib/wechat";

export function WechatFill({ text, token, target, importId, relationshipId }: { text: string; token: string; target: WechatTarget | null; importId: string; relationshipId: string }) {
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [done, setDone] = useState(false);
  const [draft, setDraft] = useState(text), [error, setError] = useState<unknown>(null);
  async function fill() {
    setBusy(true); setError(null);
    try {
      await api.post("/wechat/fill", { token, import_id: importId, relationship_id: relationshipId, text: draft });
      setDone(true); setOpen(false);
    } catch (value) { setError(value); } finally { setBusy(false); }
  }
  return <>
    <Button size="sm" disabled={!target?.allow_fill || target.filled || done || busy} onClick={() => { setDraft(text); setError(null); setOpen(true); }}>{done ? "已填入微信" : "核对并填入微信"}</Button>
    {done && <Badge tone="good">请在微信核对后自行发送</Badge>}
    <Modal open={open} onClose={() => { if (!busy) setOpen(false); }} title={`填入微信 · ${target?.contact ?? "指定好友"}`} width={580}>
      <div className="space-y-4"><Alert>填入前会再次核对好友和当前对话。输入框已有文字或对话发生变化时将停止；这一步不会按回车发送。</Alert><Field label="回复内容"><textarea className="input min-h-40" value={draft} maxLength={4000} disabled={busy} onChange={event => setDraft(event.target.value)} /></Field><ErrorBox error={error} /><div className="flex flex-wrap justify-end gap-2"><Link to="/wechat" className="btn btn-ghost">重新读取</Link><Button disabled={busy} onClick={() => setOpen(false)}>取消</Button><Button variant="primary" disabled={busy || !draft.trim()} onClick={fill}>{busy ? "核对窗口并填入…" : "确认填入输入框"}</Button></div></div>
    </Modal>
  </>;
}
