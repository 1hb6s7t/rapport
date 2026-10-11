import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import { Alert, Badge, Button, EmptyState, ErrorBox, Modal, Spinner } from "@/components/ui";
import { WorkbenchReplyDrafts, type ReviewDraft, type WithheldDraft } from "@/components/workbenchReplyDrafts";

interface Sample { id: string; context: { sender: string; text: string }[]; actual_reply: string; candidate_reply: string; base_reply: string; structured_drafts?: ReviewDraft[]; base_drafts?: ReviewDraft[]; withheld_drafts?: WithheldDraft[] }

export function ModelReplyReview({ modelId, onReviewed }: { modelId: string; onReviewed: () => void }) {
  const [open, setOpen] = useState(false), [busy, setBusy] = useState<"load" | "save" | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]), [decisions, setDecisions] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<unknown>(null);
  const [page, setPage] = useState(0);
  const [evaluationHash, setEvaluationHash] = useState<string | null>(null);
  const request = useRef(0);
  useEffect(() => () => { request.current += 1; }, [modelId]);
  async function show() {
    const revision = ++request.current;
    setOpen(true); setBusy("load"); setError(null); setDecisions({}); setSamples([]); setPage(0); setEvaluationHash(null);
    try {
      const result = await api.get<{ items: Sample[]; evaluation_hash: string }>(`/learning/models/${encodeURIComponent(modelId)}/samples`);
      if (revision === request.current) {
        if (!/^[0-9a-f]{64}$/.test(result.evaluation_hash ?? "")) throw new Error("评测版本信息缺失，请刷新后重新读取样例。");
        setSamples(result.items); setEvaluationHash(result.evaluation_hash);
      }
    } catch (err) { if (revision === request.current) setError(err); }
    finally { if (revision === request.current) setBusy(null); }
  }
  function close() {
    if (busy === "save") return;
    request.current += 1;
    setOpen(false); setBusy(null); setSamples([]); setDecisions({}); setEvaluationHash(null);
  }
  const reviewedCount = samples.filter(sample => typeof decisions[sample.id] === "boolean").length;
  async function submit() {
    if (busy || !evaluationHash || !samples.length || reviewedCount !== samples.length) return;
    const revision = ++request.current;
    setBusy("save"); setError(null);
    try {
      await api.post(`/learning/models/${encodeURIComponent(modelId)}/review`, { decisions: Object.fromEntries(samples.map(sample => [sample.id, decisions[sample.id]])), evaluation_hash: evaluationHash });
      if (revision === request.current) { setOpen(false); setSamples([]); onReviewed(); }
    } catch (err) {
      if (revision === request.current) {
        setError(err);
        if (err instanceof ApiError && err.status === 409) { setEvaluationHash(null); setSamples([]); setDecisions({}); }
      }
    }
    finally { if (revision === request.current) setBusy(null); }
  }
  return <>
    <Button size="sm" disabled={!!busy} onClick={() => void show()}>复核回复样例 · {modelId.slice(0, 8)}</Button>
    <Modal open={open} onClose={close} title="核对本人的表达与事实" width={900}>
      <div className="space-y-4">
        <Alert>逐条判断候选回复是否符合你的表达、尊重当前语境，且没有编造经历或混入其他好友的信息。与原回复不同不一定是错误；只有表达和事实都合适时才选通过。保存复核结果不会自动启用模型。</Alert>
        <ErrorBox error={error} />
        {busy && <Spinner label={busy === "load" ? "读取真实评测样例…" : "保存复核结果…"} />}
        {!busy && samples.length === 0 && <EmptyState title="没有可复核的评测样例" hint="请先完成这个版本的独立回复评测。" action={<Button onClick={() => void show()}>重新读取</Button>} />}
        {samples.slice(page * 5, (page + 1) * 5).map((sample, index) => <article key={sample.id} className="space-y-3 rounded-2xl border border-line bg-paper/70 p-4">
          <h3 className="flex flex-wrap items-center gap-2 font-medium">样例 {page * 5 + index + 1}{typeof decisions[sample.id] === "boolean" && <Badge tone={decisions[sample.id] ? "mint" : "warn"}>{decisions[sample.id] ? "通过" : "需改进"}</Badge>}</h3>
          <details className="text-sm" open><summary className="cursor-pointer">当时的对话</summary><ul className="mt-2 max-h-52 space-y-2 overflow-auto">{sample.context.map((message, i) => <li className="whitespace-pre-wrap break-words" key={i}><span className="text-ink-3">{message.sender === "me" ? "我" : message.sender === "other" ? "对方" : "未知角色"}：</span>{message.text}</li>)}</ul></details>
          <WorkbenchReplyDrafts actualReply={sample.actual_reply} baseReply={sample.base_reply} candidateReply={sample.candidate_reply} baseDrafts={sample.base_drafts} candidateDrafts={sample.structured_drafts} withheldDrafts={sample.withheld_drafts} />
          <fieldset className="flex flex-wrap gap-3 text-sm"><legend className="sr-only">样例 {page * 5 + index + 1} 的复核结果</legend>{([{ value: true, label: "整组可用候选合适，事实与好友无混用" }, { value: false, label: "不通过，需要改进" }]).map(({ value, label }) => <label key={String(value)} className="flex items-center gap-2"><input type="radio" name={`review-${modelId}-${sample.id}`} checked={decisions[sample.id] === value} disabled={!!busy || (value && sample.structured_drafts?.length === 0)} onChange={() => setDecisions(old => ({ ...old, [sample.id]: value }))} />{label}</label>)}</fieldset>
        </article>)}
        {samples.length > 5 && <div className="flex items-center justify-between gap-2 text-sm"><Button disabled={!!busy || page === 0} onClick={() => setPage(value => value - 1)}>上一页</Button><span className="text-xs text-ink-3">第 {page + 1} / {Math.ceil(samples.length / 5)} 页</span><Button disabled={!!busy || (page + 1) * 5 >= samples.length} onClick={() => setPage(value => value + 1)}>下一页</Button></div>}
        <div className="flex flex-wrap items-center justify-between gap-3"><span className="text-sm text-ink-3">已复核 {reviewedCount} / {samples.length}</span><div className="flex gap-2"><Button disabled={busy === "save"} onClick={close}>取消</Button><Button variant="primary" disabled={!!busy || !samples.length || reviewedCount !== samples.length} onClick={() => void submit()}>保存复核结果</Button></div></div>
      </div>
    </Modal>
  </>;
}
