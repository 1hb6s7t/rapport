import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { AnalysisView, PreviewView } from "@/api/types";
import { Alert, Badge, Card, ErrorBox, Spinner } from "@/components/ui";

export function PreviewPage() {
  const { analysisId = "" } = useParams();
  const nav = useNavigate();
  const [pv, setPv] = useState<PreviewView | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [accept, setAccept] = useState(false);
  const [edits, setEdits] = useState<Record<number, string>>({});
  const [editing, setEditing] = useState<number | null>(null);

  useEffect(() => {
    api.get<PreviewView>(`/analyses/${analysisId}/preview`).then(setPv).catch(setErr);
  }, [analysisId]);

  async function confirm() {
    if (!pv) return;
    setBusy(true);
    setErr(null);
    try {
      const edited_lines: Record<number, string[]> = {};
      for (const [k, v] of Object.entries(edits)) edited_lines[+k] = v.split("\n").filter((l) => l.trim());
      await api.post<AnalysisView>(`/analyses/${analysisId}/confirm`, { payload_hash: pv.payload_hash, accept_cost: accept, edited_lines });
      nav(`/progress/${analysisId}`);
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  if (err && !pv) return <ErrorBox error={err} />;
  if (!pv) return <Spinner label="准备预览…" />;
  const c = pv.cost;
  const needAccept = !c.price_known || c.over_threshold || !!pv.large_run;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">出网预览：确认后才会发送</h1>
        <p className="text-sm text-ink-2 mt-1">这一步不会调用模型。下面就是将要发送的全部内容；你可以直接编辑再确认。</p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <Card title="目的地">
          <dl className="text-sm grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-ink-3">供应商</dt><dd>{pv.destination.label}</dd>
            <dt className="text-ink-3">模型</dt><dd className="mono">{pv.destination.model}</dd>
            <dt className="text-ink-3">端点</dt><dd className="mono break-all text-xs">{pv.destination.base_url}</dd>
            <dt className="text-ink-3">接入地域</dt><dd>{pv.destination.endpoint_region}</dd>
            <dt className="text-ink-3">处理范围</dt><dd>{pv.destination.processing_scope}</dd>
          </dl>
          {!(pv.destination.ready ?? pv.destination.has_key) && <div className="mt-2"><Alert tone="warn">尚未配置 Key</Alert></div>}
        </Card>
        <Card title="费用估算">
          <div className="text-2xl font-semibold">{c.price_known && c.cny !== null ? `≈ ¥${c.cny.toFixed(3)}` : "未知"}</div>
          <p className="text-xs text-ink-3 mt-1">输入约 {c.tokens_in.toLocaleString()} tokens，输出约 {c.tokens_out.toLocaleString()} tokens · {pv.n_windows} 个提取窗口 + 1 次综合{pv.n_intent_calls ? ` + ${pv.n_intent_calls} 次语义判断` : ""}{pv.n_calls ? `，共约 ${pv.n_calls} 次调用` : ""}{pv.est_minutes ? `，预计 ${pv.est_minutes} 分钟` : ""}</p>
          {pv.large_run && <div className="mt-2"><Alert tone="warn" title="长材料分析">{pv.large_run_note} 进度逐窗保存，中断后重试会从断点继续。</Alert></div>}
          <p className="text-xs text-ink-3 mt-1">{c.note}</p>
          {c.over_threshold && <div className="mt-2"><Badge tone="accent">超过预警阈值</Badge></div>}
        </Card>
        <Card title="脱敏与字段">
          <p className="text-sm">已替换：昵称 {pv.redaction.names_replaced} 处{Object.entries(pv.redaction.counts).map(([k, v]) => `，${k} ${v} 处`).join("")}</p>
          <p className="text-xs text-ink-3 mt-2">发送：{pv.fields_sent.join("、")}</p>
          <p className="text-xs text-ink-3 mt-1">不发送：{pv.fields_not_sent.join("、")}</p>
        </Card>
      </div>
      <Card title={`将发送的正文（${pv.n_messages} 条，A = 我，B = 对方）`} right={<span className="text-xs text-ink-3">点击窗口可编辑</span>}>
        <div className="space-y-3 max-h-[50vh] overflow-auto">
          {pv.windows.map((lines, i) => (
            <div key={i} className="rounded-lg border border-line">
              <div className="flex items-center justify-between px-3 py-1 text-xs text-ink-3 bg-paper rounded-t-lg">
                <span>窗口 {i + 1} / {pv.windows.length}{edits[i] !== undefined && " · 已编辑"}</span>
                <button className="underline" onClick={() => setEditing(editing === i ? null : i)}>{editing === i ? "完成" : "编辑"}</button>
              </div>
              {editing === i ? (
                <textarea className="input mono text-xs min-h-[200px] rounded-t-none border-0" value={edits[i] ?? lines.join("\n")} onChange={(e) => setEdits({ ...edits, [i]: e.target.value })} aria-label={`窗口 ${i + 1} 内容`} />
              ) : (
                <pre className="mono text-xs whitespace-pre-wrap px-3 py-2 max-h-64 overflow-auto">{(edits[i] ?? lines.join("\n"))}</pre>
              )}
            </div>
          ))}
        </div>
      </Card>
      <Card>
        <div className="flex flex-wrap items-center gap-4">
          {needAccept && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />
              我了解{pv.large_run ? "这是长材料分析（数百次调用）" : !c.price_known ? "价格未知" : "费用超过预警阈值"}，接受由我的账户承担费用与时间
            </label>
          )}
          <div className="ml-auto flex gap-2">
            <button className="btn btn-ghost" onClick={() => nav("/profile")}>返回，不发送</button>
            <button className="btn btn-primary" disabled={busy || (needAccept && !accept) || !(pv.destination.ready ?? pv.destination.has_key)} onClick={confirm}>确认发送并开始分析</button>
          </div>
        </div>
        <ErrorBox error={err} />
      </Card>
    </div>
  );
}
