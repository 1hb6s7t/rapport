import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { ReplyPack, ReplyPreview } from "@/api/types";
import { RelationshipPicker } from "@/components/RelationshipPicker";
import { PasteToChat } from "@/components/PasteToChat";
import { WechatFill } from "@/components/WechatFill";
import { DispatchPreview, type DispatchItem } from "@/components/DispatchPreview";
import { WechatStickerSearch } from "@/components/WechatStickerSearch";
import { useWechatTarget } from "@/lib/wechat";
import { Alert, Badge, Card, Empty, ErrorBox, Field, Spinner } from "@/components/ui";
import { useFlow } from "@/store";

const STYLES: [string, string][] = [
  ["casual", "轻松"],
  ["sincere", "真诚"],
  ["direct", "直接"],
  ["warm", "温柔"],
  ["humorous", "幽默"],
  ["brief", "简洁"],
];

function DraftDispatch({ token, relationshipId, importId, initial }: {
  token: string; relationshipId: string; importId: string; initial: DispatchItem[];
}) {
  const [items, setItems] = useState(initial);
  function move(index: number, offset: number) {
    const next = [...items];
    [next[index], next[index + offset]] = [next[index + offset], next[index]];
    setItems(next);
  }
  return <div className="mt-3 space-y-2 border-t border-[var(--border)] pt-3">
    {items.length > 1 && <ol className="space-y-1 text-xs" aria-label="消息发送顺序">{items.map((item, index) => <li key={`${item.asset_id ?? 'text'}-${index}`} className="flex items-center gap-2">
      <span className="min-w-0 flex-1 truncate">{index + 1}. {item.kind === 'text' ? item.text : item.kind === 'image' ? '图片素材' : '表情素材'}</span>
      <button className="btn btn-ghost" aria-label={`第${index + 1}项上移`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
      <button className="btn btn-ghost" aria-label={`第${index + 1}项下移`} disabled={index === items.length - 1} onClick={() => move(index, 1)}>↓</button>
      <button className="btn btn-ghost" onClick={() => setItems(items.filter((_, i) => i !== index))}>移除</button>
    </li>)}</ol>}
    <DispatchPreview captureToken={token} relationshipId={relationshipId} importId={importId} items={items} />
  </div>;
}

export function RepliesPage() {
  const [pack, setPack] = useState<ReplyPack | null>(null);
  const [preview, setPreview] = useState<ReplyPreview | null>(null);
  const [accept, setAccept] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [styles, setStyles] = useState<string[]>(["casual", "sincere", "direct"]);
  const [mode, setMode] = useState<"free" | "strict">("free");
  const [memory, setMemory] = useState(true);
  const [skill, setSkill] = useState(true);
  const [flow] = useFlow();
  const relationship = flow.relationshipId;
  const [importId, setImportId] = useState(flow.importId ?? "");
  const [goal, setGoal] = useState("");
  const scope = useRef("");
  scope.current = `${relationship}:${importId}:${skill}`;
  const wechat = useWechatTarget(pack?.context?.import_id ?? importId, relationship);
  useEffect(() => { setPack(null); setPreview(null); setAccept(false); }, [relationship, importId, skill]);
  useEffect(() => { setImportId(flow.importId ?? ""); setGoal(""); }, [relationship, flow.importId]);
  const body = () => ({ import_id: importId || null, relationship_id: relationship, styles, memory_enabled: memory, mode, goal: goal || null, skill: mode === "free" && skill ? "goutoujunshi" : "off" });

  async function generate() {
    const started = scope.current;
    setBusy(true);
    setErr(null);
    setPack(null);
    try {
      if (mode === "strict") {
        const value = await api.post<ReplyPack>("/replies", body());
        if (started === scope.current) setPack(value);
      } else {
        setAccept(false);
        const value = await api.post<ReplyPreview>("/replies/preview", body());
        if (started === scope.current) setPreview(value);
      }
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!preview) return;
    const started = scope.current;
    setBusy(true);
    setErr(null);
    try {
      const value = await api.post<ReplyPack>("/replies", { ...body(), ticket_id: preview.ticket_id, payload_hash: preview.payload_hash, accept_cost: accept });
      if (started === scope.current) { setPack(value); setPreview(null); }
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">AI 代回复草稿</h1>
        <p className="text-sm text-ink-2 mt-1">
          先生成<strong>草稿</strong>，核对后可复制、填入微信，或预览整组并确认发送。草稿里出现的日记内容会标注来源与版本；
          没有日记依据的生活细节不会写进草稿。你也可以完全关掉日记记忆，只生成普通话术。
        </p>
      </div>

      <div className="flex flex-wrap gap-3 items-center"><Link to="/wechat" className="btn btn-soft">从微信数据库读取对话 ↗</Link>{wechat.token && <Badge tone={wechat.target ? "good" : "warn"}>{wechat.target ? `已绑定微信好友：${wechat.target.contact}` : "微信绑定待核对 / 已过期"}</Badge>}</div>
      {wechat.token && !wechat.target?.allow_fill && <Alert>如需填入草稿或发送选中的表情，请在「设置 → 微信与桌面」里开启「允许确认后操作微信」。窗口绑定失效时请重新准备回复。</Alert>}
      <ErrorBox error={wechat.error} />
      {wechat.token && wechat.target?.import_id && <WechatStickerSearch
        key={`${relationship}:${wechat.target.import_id}:${wechat.token}`}
        captureToken={wechat.token} relationshipId={relationship} importId={wechat.target.import_id}
        enabled={wechat.target.allow_fill && !wechat.target.filled} />}
      <Card title="生成设置">
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="风格（可多选）">
            <div className="flex flex-wrap gap-2">
              {STYLES.map(([v, l]) => (
                <label key={v} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={styles.includes(v)}
                    onChange={(e) => setStyles(e.target.checked ? [...styles, v] : styles.filter((s) => s !== v))}
                  />
                  {l}
                </label>
              ))}
            </div>
          </Field>
          <Field label="模式" hint="严格模式不调用模型，只用你批准的日记短句">
            <select className="input" value={mode} onChange={(e) => setMode(e.target.value as "free" | "strict")}>
              <option value="free">自由生成（带防编造校验）</option>
              <option value="strict">严格模式（只用已批准短句）</option>
            </select>
          </Field>
          <RelationshipPicker label="对象" hint="用该对象最近一次确认的材料起草；日记按该对象的授权范围检索。" />
          <Field label="使用日记记忆">
            <label className="flex items-center gap-2 text-sm mt-2">
              <input type="checkbox" checked={memory} onChange={(e) => setMemory(e.target.checked)} />
              本次生成允许读取日记
            </label>
          </Field>
          <Field label="狗头军师聊天指导" hint="帮助承接情绪、展开话题和把握分寸">
            <label className="flex items-center gap-2 text-sm mt-2">
              <input type="checkbox" checked={skill} disabled={busy || mode === "strict"} onChange={(e) => setSkill(e.target.checked)} />
              为本次草稿启用聊天指导
            </label>
          </Field>
          <Field label="指定导入（留空即用最近一次已确认的）" hint="想指定某次分析的材料时，填它的 import id">
            <input className="input mono" value={importId} onChange={(e) => setImportId(e.target.value)} placeholder="import id" />
          </Field>
          <Field label="本次目标（可选）">
            <input className="input" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="例如：自然地约一次见面" />
          </Field>
        </div>
        <div className="mt-3">
          <button className="btn btn-primary" disabled={busy || styles.length === 0} onClick={generate}>{mode === "strict" ? "生成草稿" : "预览将发送的内容"}</button>
        </div>
      </Card>

      {busy && <Spinner label={preview ? "生成中…" : "准备中…"} />}
      <ErrorBox error={err} />

      {preview && !pack && (
        <Card title={preview.destination.connection_mode === "local" ? "本地生成预览" : "外部模型请求预览"} right={<span className="text-xs text-ink-3">{Math.floor(preview.expires_in_s / 60)} 分钟内有效</span>}>
          <div className="grid gap-4 md:grid-cols-3 text-sm">
            <div>
              <div className="text-xs text-ink-3 mb-1">目的地</div>
              <div>{preview.destination.label} · <span className="mono">{preview.destination.model}</span></div>
              <div className="text-xs text-ink-3">{preview.destination.connection_mode === "local" ? "本地模式" : "云端模式"} · {preview.destination.endpoint_region}</div>
            </div>
            <div>
              <div className="text-xs text-ink-3 mb-1">费用估算</div>
              <div className="text-xl font-semibold">{preview.cost.price_known && preview.cost.cny !== null ? `≈ ¥${preview.cost.cny.toFixed(3)}` : "未知"}</div>
              <div className="text-xs text-ink-3">输入约 {preview.cost.tokens_in} · 输出约 {preview.cost.tokens_out} tokens</div>
            </div>
            <div>
              <div className="text-xs text-ink-3 mb-1">日记</div>
              <div className={preview.diary_refs.length ? "text-warn" : ""}>{preview.diary_refs.length ? `${preview.diary_refs.length} 条日记、${preview.n_diary_chars} 字会随请求发出` : "本次不发送任何日记内容"}</div>
            </div>
          </div>
          {preview.diary_lines.length > 0 && (
            <div className="mt-3">
              <div className="text-xs text-ink-3 mb-1">将发送的日记原文（这是最私密的部分，请逐条确认）</div>
              <ul className="text-sm space-y-1">{preview.diary_lines.map((l) => <li key={l} className="rounded bg-warn-soft/40 px-2 py-1">{l}</li>)}</ul>
            </div>
          )}
          <details className="mt-3 text-xs">
            <summary className="cursor-pointer text-ink-3">查看完整生成请求：对话、档案与所选记忆</summary>
            <pre className="mono whitespace-pre-wrap mt-1 max-h-48 overflow-auto">{preview.lines.join("\n")}</pre>
          </details>
          <p className="text-xs text-ink-3 mt-2">发送：{preview.fields_sent.join("、")}；不发送：{preview.fields_not_sent.join("、")}</p>
          <div className="flex flex-wrap items-center gap-4 mt-3">
            {(!preview.cost.price_known || preview.cost.over_threshold) && (
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />我了解{!preview.cost.price_known ? "价格未知" : "费用超过预警阈值"}，接受费用</label>
            )}
            <div className="ml-auto flex gap-2">
              <button className="btn btn-ghost" onClick={() => setPreview(null)}>取消，不发送</button>
              <button className="btn btn-primary" disabled={busy || ((!preview.cost.price_known || preview.cost.over_threshold) && !accept) || !(preview.destination.ready ?? preview.destination.has_key)} onClick={confirm}>确认发送并生成</button>
            </div>
          </div>
        </Card>
      )}

      {pack && (
        <>
          {pack.context && pack.context.messages.length > 0 && (
            <Card title="草稿回应的是这段对话">
              <ul className="text-sm space-y-1">
                {pack.context.messages.map((m) => (
                  <li key={m.id} className={m.id === pack.context?.target_msg_id ? "font-medium" : "text-ink-2"}>
                    <Badge>{m.sender === "me" ? "我" : "对方"}</Badge> {m.text}{m.id === pack.context?.target_msg_id && <span className="text-xs text-ink-3"> ◀ 要回复的这条</span>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
          <Alert tone={pack.memory_enabled ? "info" : "warn"}>
            {pack.memory_enabled ? `本次使用日记记忆：${pack.memories_used.length} 条` : "本次未使用日记记忆"}
          </Alert>
          {pack.abstain_reason && <Alert tone="warn">{pack.abstain_reason}</Alert>}
          {pack.drafts.length === 0 ? <Empty>没有可用草稿</Empty> : (
            <div className="grid gap-3 md:grid-cols-2">
              {pack.drafts.map((d, i) => (
                <Card key={`${d.style}-${i}`} title={<span className="flex items-center gap-2">{STYLES.find(([v]) => v === d.style)?.[1] ?? d.style}{d.mode === "strict_slot" && <Badge tone="info">严格模式</Badge>}{d.verified && !d.needs_review && <Badge tone="good">已核对</Badge>}{d.needs_review && <Badge tone="warn">需核对</Badge>}</span>}>
                  <blockquote className="border-l-2 border-accent pl-3 text-sm whitespace-pre-wrap">{d.text}</blockquote>
                  <div className="mt-2 flex items-center gap-2">
                    <button className="btn btn-ghost py-0.5 text-xs" onClick={() => navigator.clipboard?.writeText(d.text)}>复制</button>
                    {wechat.token ? <WechatFill text={d.text} token={wechat.token} target={wechat.target} importId={pack.context?.import_id ?? importId} relationshipId={relationship} /> : <PasteToChat text={d.text} source="reply" />}
                    {d.memory_refs.map((r) => <Badge key={r.id} tone="neutral">日记 {r.id.slice(0, 6)} v{r.revision}</Badge>)}
                  </div>
                  {d.why && <p className="text-xs text-ink-3 mt-1">为什么：{d.why}</p>}
                  {d.risk_notes.length > 0 && (
                    <ul className="mt-1 text-xs text-warn space-y-0.5">
                      {d.risk_notes.map((r) => <li key={r}>⚠ {r}</li>)}
                    </ul>
                  )}
                  {d.needs_review && d.unbacked_claims.length > 0 && <p className="text-xs text-warn mt-1">草稿里这些“我做过的事”没有日记依据：{d.unbacked_claims.join("；")}</p>}
                  <p className="text-xs text-ink-3 mt-1">{d.verification_note}</p>
                  {wechat.token && wechat.target?.allow_fill && (pack.context?.import_id ?? importId) && <DraftDispatch key={`${relationship}:${pack.context?.import_id}:${i}:${d.text}`} token={wechat.token} relationshipId={relationship} importId={pack.context?.import_id ?? importId} initial={d.items?.length ? d.items : [{ kind: "text", text: d.text }]} />}
                </Card>
              ))}
            </div>
          )}
          <Alert tone="good">{pack.send_note}</Alert>
          {pack.fabricated_note && <p className="text-xs text-ink-3">{pack.fabricated_note}</p>}
          {Object.keys(pack.dropped_memories).length > 0 && (
            <p className="text-xs text-ink-3">被丢弃的记忆：{Object.entries(pack.dropped_memories).map(([k, v]) => `${k}×${v}`).join("，")}</p>
          )}
        </>
      )}
    </div>
  );
}
