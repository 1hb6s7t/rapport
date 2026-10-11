import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { AnalysisView } from "@/api/types";
import { Alert, Card, ErrorBox, Spinner } from "@/components/ui";
import { STATUS_LABELS } from "@/i18n/labels";
import { setFlow } from "@/store";

const STAGES = ["queued", "extracting", "synthesizing", "validating", "intent", "assembling", "completed"];

export function ProgressPage() {
  const { analysisId = "" } = useParams();
  const nav = useNavigate();
  const [a, setA] = useState<AnalysisView | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const v = await api.get<AnalysisView>(`/analyses/${analysisId}`);
        if (!alive) return;
        setA(v);
        if ((v.status === "completed" || v.status === "insufficient_data") && v.report_id) {
          setFlow({ reportId: v.report_id });
          nav(`/report/${v.report_id}`, { replace: true });
          return;
        }
        if (["failed", "cancelled", "interrupted", "created"].includes(v.status)) return;
        timer.current = window.setTimeout(poll, 1200);
      } catch (e) {
        if (alive) setErr(e);
      }
    }
    poll();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [analysisId, nav]);

  if (err) return <ErrorBox error={err} />;
  if (!a) return <Spinner label="读取任务…" />;
  const idx = STAGES.indexOf(a.status);
  const terminal = ["failed", "cancelled", "interrupted"].includes(a.status);

  return (
    <div className="max-w-2xl mx-auto space-y-4">
      <Card title={`分析进行中 · ${a.mode === "byok" ? "AI 模式" : "规则模式"}`}>
        <ol className="flex flex-wrap gap-2 text-xs mb-4" aria-label="阶段">
          {STAGES.filter((s) => a.mode === "byok" || !["extracting", "synthesizing", "validating", "intent"].includes(s)).map((s) => {
            const i = STAGES.indexOf(s);
            const state = i < idx ? "done" : i === idx ? "now" : "todo";
            return (
              <li key={s} className={`px-2 py-1 rounded-md ${state === "done" ? "bg-good-soft text-good" : state === "now" ? "bg-ink text-white" : "bg-paper text-ink-3"}`}>
                {STATUS_LABELS[s]}
              </li>
            );
          })}
        </ol>
        <div className="text-sm">
          <div className="font-medium">{STATUS_LABELS[a.status] ?? a.status}</div>
          <div className="text-ink-2">{a.message}</div>
          {(a.status === "extracting" || a.status === "intent") && a.stage_total > 0 && (
            <div className="mt-2">
              <div className="h-2 bg-paper rounded overflow-hidden"><div className="h-full bg-ink transition-all" style={{ width: `${(a.stage_done / a.stage_total) * 100}%` }} /></div>
              <div className="text-xs text-ink-3 mt-1">{a.stage_done} / {a.stage_total} {a.status === "intent" ? "处" : "段"}</div>
            </div>
          )}
        </div>
        {!terminal && a.status !== "created" && <div className="mt-3"><Spinner label="真实进度，不做假动画" /></div>}
        {a.error && <div className="mt-3"><Alert tone="warn" title={`[${a.error.code}]`}>{a.error.msg}</Alert></div>}
        {a.status === "created" && <div className="mt-3"><Alert tone="info">该任务尚未确认出网。<a href={`/preview/${a.id}`}>去预览并确认</a></Alert></div>}
        <div className="flex gap-2 mt-4">
          {!terminal && a.status !== "created" && (
            <button className="btn btn-ghost" onClick={() => api.post(`/analyses/${a.id}/cancel`).then((v) => setA(v as AnalysisView))}>取消（在途调用可能已计费）</button>
          )}
          {(a.status === "interrupted" || a.status === "failed") && (
            <button className="btn btn-primary" onClick={() => api.post<AnalysisView>(`/analyses/${a.id}/retry`).then(setA).catch(setErr)}>重试（复用已完成分段）</button>
          )}
          <button className="btn btn-ghost" onClick={() => nav("/profile")}>返回</button>
        </div>
      </Card>
    </div>
  );
}
