import { Alert, Badge } from "@/components/ui";

export interface ReviewDraft { style: string; text: string; needs_review?: boolean; risk_notes?: string[] }
export interface WithheldDraft { style: string; reason: string }
const STYLES: Record<string, string> = { casual: "轻松版", sincere: "真诚版", direct: "直接版" };

function DraftGroup({ title, drafts, legacy, candidate = false }: { title: string; drafts?: ReviewDraft[]; legacy: string; candidate?: boolean }) {
  return <section className="space-y-2 rounded-xl border border-line bg-card/60 p-3">
    <h4 className={`text-sm font-medium ${candidate ? "text-brand-ink" : "text-ink-2"}`}>{title}{drafts && ` · ${drafts.length} 条可用候选`}</h4>
    {drafts ? drafts.length ? drafts.map((draft, index) => <article key={`${draft.style}-${index}`} className="space-y-2 border-t border-line pt-2 text-sm">
      <div className="flex flex-wrap items-center gap-2"><Badge tone={candidate ? "brand" : "neutral"}>{STYLES[draft.style] ?? draft.style}</Badge>{draft.needs_review && <Badge tone="warn">仍需复核</Badge>}</div>
      <p className="whitespace-pre-wrap break-words">{draft.text || "没有生成文字"}</p>
      {draft.risk_notes?.length ? <ul className="list-disc space-y-1 pl-5 text-xs text-warn">{draft.risk_notes.map((note, i) => <li key={i}>{note}</li>)}</ul> : null}
    </article>) : <p className="text-sm text-warn">没有通过检查的可用候选。</p> : <>
      <p className="whitespace-pre-wrap break-words text-sm">{legacy || "没有生成回复"}</p>
      <p className="text-xs text-ink-3">这是旧评测的单条记录，未记录整组候选。</p>
    </>}
  </section>;
}

export function WorkbenchReplyDrafts({ actualReply, baseReply, candidateReply, baseDrafts, candidateDrafts, withheldDrafts }: {
  actualReply: string; baseReply: string; candidateReply: string; baseDrafts?: ReviewDraft[]; candidateDrafts?: ReviewDraft[]; withheldDrafts?: WithheldDraft[];
}) {
  return <div className="space-y-3">
    <section className="rounded-xl bg-paper p-3 text-sm"><h4 className="text-ink-3">当时我实际说的</h4><p className="mt-1 whitespace-pre-wrap break-words">{actualReply || "未记录"}</p></section>
    <div className="grid gap-3 md:grid-cols-2"><DraftGroup title="微调前" drafts={baseDrafts} legacy={baseReply} /><DraftGroup title="候选模型" drafts={candidateDrafts} legacy={candidateReply} candidate /></div>
    {withheldDrafts?.length ? <Alert tone="warn" title={`本组拦截了 ${withheldDrafts.length} 条草稿`}><span>这些草稿没有进入可用候选，也不会展示或用于发送。</span><ul className="mt-2 list-disc space-y-1 pl-5">{withheldDrafts.map((draft, index) => <li key={`${draft.style}-${index}`}>{STYLES[draft.style] ?? draft.style}：{draft.reason}</li>)}</ul></Alert> : null}
    {(candidateDrafts !== undefined || withheldDrafts !== undefined) && <p className="text-xs leading-relaxed text-ink-3">请核对整组可用候选。格式检查通过只说明返回结构可用，不代表原始生成没有事实风险；上面会另列已拦截草稿及原因。</p>}
  </div>;
}
