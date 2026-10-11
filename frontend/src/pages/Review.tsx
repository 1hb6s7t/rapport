import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { ImportView, Message, Sender } from "@/api/types";
import { Alert, Badge, Card, ErrorBox, Field, Spinner } from "@/components/ui";
import { useFlow } from "@/store";

const SENDER_LABEL: Record<Sender, string> = { me: "我", other: "对方", system: "系统", unknown: "未知" };

export function ReviewPage() {
  const nav = useNavigate();
  const location = useLocation();
  const forReply = new URLSearchParams(location.search).get("next") === "replies";
  const [flow] = useFlow();
  const [imp, setImp] = useState<ImportView | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [range, setRange] = useState<[number, number]>([0, 0]);
  const [pending, setPending] = useState<Record<string, Partial<Message>>>({});
  const [filter, setFilter] = useState<"all" | "unknown">("all");
  const [page, setPage] = useState(0);
  const PAGE = 200;

  useEffect(() => {
    if (!flow.importId) return;
    api.get<ImportView>(`/imports/${flow.importId}`).then((v) => {
      setImp(v);
      setRange([v.range_start ?? 0, v.range_end ?? v.n_messages - 1]);
    }).catch(setErr);
  }, [flow.importId]);

  const msgs = imp?.messages ?? [];
  const view = useMemo(() => (filter === "unknown" ? msgs.filter((m) => m.sender === "unknown") : msgs), [msgs, filter]);
  const paged = view.slice(page * PAGE, (page + 1) * PAGE);
  const nUnknownInRange = msgs.filter((m) => m.seq >= range[0] && m.seq <= range[1] && m.sender === "unknown").length;
  const inRange = msgs.filter((m) => m.seq >= range[0] && m.seq <= range[1] && (m.sender === "me" || m.sender === "other"));
  const untimed = inRange.filter((m) => !m.ts || m.ts_precision === "day" || m.ts_precision === "none").length;

  function edit(id: string, patch: Partial<Message>) {
    setPending((p) => ({ ...p, [id]: { ...p[id], ...patch } }));
    setImp((v) => v && { ...v, messages: v.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) });
  }

  async function save(extra: Record<string, unknown> = {}) {
    if (!imp) return false;
    setBusy(true);
    setErr(null);
    try {
      const body = { messages: Object.entries(pending).map(([id, p]) => ({ id, sender: p.sender, text: p.text, type: p.type })), ...extra };
      const v = await api.patch<ImportView>(`/imports/${imp.id}`, body);
      setImp(v);
      setPending({});
      return true;
    } catch (e) {
      setErr(e);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!imp) return;
    if (Object.keys(pending).length && !(await save())) return;
    setBusy(true);
    setErr(null);
    try {
      const v = await api.post<ImportView>(`/imports/${imp.id}/confirm`, { range_start: range[0], range_end: range[1] });
      setImp((old) => old && { ...old, ...v, messages: old.messages });
      nav(forReply ? "/replies" : "/profile");
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  if (!flow.importId) return <Alert tone="info">还没有导入材料。<a href="/import">去导入</a></Alert>;
  if (!imp) return err ? <ErrorBox error={err} /> : <Spinner />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">校正与确认</h1>
          <p className="text-sm text-ink-2 mt-1">解析器：{imp.parser_id} · {imp.n_messages} 条消息 · {imp.has_reliable_time ? "有可靠时间" : "缺少可靠时间（时间类指标将不可用）"}</p>
        </div>
        <div className="flex gap-2">
          <button className="btn btn-ghost" disabled={busy} onClick={() => save({ swap_roles: true })}>⇄ 互换我/对方</button>
          <button className="btn btn-primary" disabled={busy || nUnknownInRange > 0 || inRange.length === 0} onClick={confirm}>{forReply ? "确认区间，进入代回复" : "确认区间，下一步"}</button>
        </div>
      </div>

      {imp.warnings.length > 0 && (
        <div className="space-y-2">
          {imp.warnings.map((w) => <Alert key={w.code + w.msg} tone="info" title={w.code}>{w.msg}</Alert>)}
        </div>
      )}

      <Card title="参与者与区间">
        <div className="grid gap-3 md:grid-cols-[1fr_1fr_2fr]">
          <Field label="我（本地别名）">
            <input className="input" value={imp.participants.me} onChange={(e) => setImp({ ...imp, participants: { ...imp.participants, me: e.target.value } })} onBlur={() => save({ participants: imp.participants })} />
          </Field>
          <Field label="对方（本地别名）">
            <input className="input" value={imp.participants.other} onChange={(e) => setImp({ ...imp, participants: { ...imp.participants, other: e.target.value } })} onBlur={() => save({ participants: imp.participants })} />
          </Field>
          <div>
            <span className="label">分析区间（按消息序号）</span>
            <div className="flex items-center gap-2">
              <input type="number" className="input w-24" min={0} max={range[1]} value={range[0]} onChange={(e) => setRange([Math.max(0, Math.min(+e.target.value, range[1])), range[1]])} aria-label="起始序号" />
              <input type="range" min={0} max={imp.n_messages - 1} value={range[0]} onChange={(e) => setRange([Math.min(+e.target.value, range[1]), range[1]])} className="flex-1" aria-label="起始" />
              <input type="range" min={0} max={imp.n_messages - 1} value={range[1]} onChange={(e) => setRange([range[0], Math.max(+e.target.value, range[0])])} className="flex-1" aria-label="结束" />
              <input type="number" className="input w-24" min={range[0]} max={imp.n_messages - 1} value={range[1]} onChange={(e) => setRange([range[0], Math.min(imp.n_messages - 1, Math.max(+e.target.value, range[0]))])} aria-label="结束序号" />
            </div>
            <div className="text-xs text-ink-3 mt-1 flex flex-wrap gap-3">
              <span>区间内互动消息 {inRange.length} 条（我 {inRange.filter((m) => m.sender === "me").length} / 对方 {inRange.filter((m) => m.sender === "other").length}）</span>
              <span>缺时间 {inRange.length ? Math.round((untimed / inRange.length) * 100) : 0}%</span>
              {inRange.length > 2000 && <Badge tone="accent">超过 2000 条：AI 模式会分约 {Math.ceil(inRange.length / 110)} 个窗口逐段发送，预览页会给出调用次数与费用</Badge>}
              {nUnknownInRange > 0 && <Badge tone="warn">{nUnknownInRange} 条发送者未确认</Badge>}
            </div>
          </div>
        </div>
      </Card>

      <Card
        title="消息列表"
        right={
          <div className="flex items-center gap-2 text-sm">
            <select className="input py-1 w-auto" value={filter} onChange={(e) => { setFilter(e.target.value as "all" | "unknown"); setPage(0); }} aria-label="筛选">
              <option value="all">全部</option>
              <option value="unknown">仅未知发送者</option>
            </select>
            {Object.keys(pending).length > 0 && <button className="btn btn-primary py-1" disabled={busy} onClick={() => save()}>保存 {Object.keys(pending).length} 处修改</button>}
          </div>
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-ink-3 text-left">
              <tr><th className="py-1 pr-2">#</th><th className="pr-2">时间</th><th className="pr-2">发送者</th><th className="pr-2">类型</th><th>内容</th></tr>
            </thead>
            <tbody>
              {paged.map((m) => {
                const out = m.seq < range[0] || m.seq > range[1];
                return (
                  <tr key={m.id} className={`border-t border-line ${out ? "opacity-40" : ""} ${m.sender === "unknown" ? "bg-warn-soft/40" : ""}`}>
                    <td className="py-1 pr-2 mono text-xs text-ink-3">{m.seq}</td>
                    <td className="pr-2 mono text-xs whitespace-nowrap">{fmtTs(m)}</td>
                    <td className="pr-2">
                      <select className="input py-0.5 w-auto text-xs" value={m.sender} onChange={(e) => edit(m.id, { sender: e.target.value as Sender })} aria-label={`第 ${m.seq} 条发送者`}>
                        {(["me", "other", "system", "unknown"] as Sender[]).map((s) => <option key={s} value={s}>{SENDER_LABEL[s]}</option>)}
                      </select>
                    </td>
                    <td className="pr-2 text-xs text-ink-3">{m.type}</td>
                    <td>
                      {m.type === "text" || m.type === "unknown" ? (
                        <input className="input py-0.5" value={m.text ?? ""} onChange={(e) => edit(m.id, { text: e.target.value })} aria-label={`第 ${m.seq} 条内容`} />
                      ) : (
                        <span className="text-ink-3">[{m.type}]{typeof m.meta.placeholder === "string" ? ` ${m.meta.placeholder}` : ""}</span>
                      )}
                      {m.quoted_text && <div className="text-xs text-ink-3 mt-0.5">引用：{m.quoted_text}</div>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {view.length > PAGE && (
          <div className="flex items-center justify-between text-sm mt-3">
            <button className="btn btn-ghost py-1" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
            <span className="text-ink-3">{page + 1} / {Math.ceil(view.length / PAGE)}</span>
            <button className="btn btn-ghost py-1" disabled={(page + 1) * PAGE >= view.length} onClick={() => setPage(page + 1)}>下一页</button>
          </div>
        )}
      </Card>
      <ErrorBox error={err} />
    </div>
  );
}

function fmtTs(m: Message): string {
  if (!m.ts) return "—";
  const d = new Date(m.ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (m.ts_precision === "day") return `${date} (日)`;
  if (m.ts_precision === "label") return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())} (标签)`;
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}${m.ts_precision === "second" ? ":" + pad(d.getSeconds()) : ""}`;
}
