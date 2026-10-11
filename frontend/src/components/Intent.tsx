import { useState } from "react";
import type { IntentReading, Report } from "@/api/types";
import { EvidenceChips } from "@/components/Evidence";
import { FeedbackButtons } from "@/components/Feedback";
import { Badge, Card } from "@/components/ui";

/** Tags whose display is held to a higher bar: the model over-reads coldness (measured 2026-09-15). */
export const NEGATIVE_PRAGMATIC = ["真生气", "反讽", "委婉拒绝"];
export const NEGATIVE_EMOTIONS = ["不满", "伤心"];
const ALL_PRAGMATIC = ["真生气", "撒娇假生气", "调侃", "反讽", "委婉拒绝", "寻求安慰", "信息确认", "分享陈述", "询问", "观点评价", "抱怨", "同意接受", "不确定"];
const METHOD_LABELS: Record<string, string> = { structbert: "单句模型", bge_knn: "近邻检索", centroid: "类别质心", markers: "礼貌标记" };
/** Readings produced on this machine (no model call, no feedback loop into the AI prompts). */
const isOffline = (r: IntentReading) => r.source === "local_model" || r.source === "grounded";

export function splitTags(reading: IntentReading): { main: string[]; negative: string[] } {
  const neg = new Set([...NEGATIVE_PRAGMATIC, ...NEGATIVE_EMOTIONS]);
  const all = [...reading.pragmatic_tags, ...reading.emotions];
  return { main: all.filter((t) => !neg.has(t)), negative: all.filter((t) => neg.has(t)) };
}

function TagList({ tags, tone }: { tags: string[]; tone?: "neutral" | "warn" }) {
  if (!tags.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {tags.map((t) => <Badge key={t} tone={tone ?? "neutral"}>{t}</Badge>)}
    </span>
  );
}

export function IntentList({ report, onPick }: { report: Report; onPick: (id: string) => void }) {
  const readings = report.intent_readings ?? [];
  const [showNegative, setShowNegative] = useState(true);  // v0.8: negatives shown by default; the toggle hides them
  if (!readings.length) {
    return <p className="text-sm text-ink-3">本次没有可判断的对方文本。规则模式默认不生成语义判断；在「设置 → 分析与个性化」里开启「离线分析层」后，可离线补充逐句判断（BGE 近邻检索 + 情绪小模型 + 礼貌标记）。</p>;
  }
  const withNegative = readings.filter((r) => splitTags(r).negative.length > 0);
  return (
    <div className="space-y-3">
      <p className="text-xs text-ink-3">
        每条判断都基于上下文，允许同时成立多个标签；证据不足时保留候选解释而不下结论。
        {withNegative.length > 0 && `其中 ${withNegative.length} 条含负面判断。`}
      </p>
      <div className="flex items-center gap-2">
        <button className="btn btn-ghost py-1 text-xs" onClick={() => setShowNegative((v) => !v)} aria-expanded={showNegative}>
          {showNegative ? "隐藏负面判断" : `显示负面判断（${withNegative.length}）`}
        </button>
        {withNegative.length > 0 && <span className="text-xs text-ink-3">负面判断容易高估：看每条旁边的片段数与一致度再下结论</span>}
      </div>
      <ul className="space-y-3">
        {readings.map((r) => {
          const { main, negative } = splitTags(r);
          const tags = showNegative ? [...main, ...negative] : main;
          return (
            <li key={r.msg_id + r.episode_id} className="rounded-lg border border-line p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge>{r.speaker === "me" ? "我" : "对方"}</Badge>
                <span className="font-medium">{r.surface_text || "（非文本消息）"}</span>
                <TagList tags={tags} tone="neutral" />
                {r.source === "local_model" && <Badge tone="info">本地小模型</Badge>}
                {r.source === "grounded" && <Badge tone="info">离线分析层</Badge>}
                {r.boundary_status !== "none" && <Badge tone="warn">边界信号</Badge>}
                {r.ambiguous && <Badge tone="accent">材料不足</Badge>}
                {r.post_hoc && <Badge>事后解释（含后文）</Badge>}
                {!isOffline(r) && splitTags(r).negative.length > 0 && <Badge tone={r.independent_units >= 2 ? "neutral" : "warn"}>{r.independent_units} 个片段支持</Badge>}
                {r.source === "grounded" && splitTags(r).negative.length > 0 && <Badge tone={r.independent_units >= 2 ? "neutral" : "warn"}>{r.independent_units} 个信号支持</Badge>}
                {(r.samples ?? 1) >= 2 && r.consistency != null && (
                  <Badge tone={r.consistency >= 1 ? "good" : r.consistency >= 0.6 ? "neutral" : "warn"}>{r.samples} 次判断中 {Math.round(r.consistency * (r.samples ?? 1))} 次一致</Badge>
                )}
                {!isOffline(r) && <FeedbackButtons kind="intent" target={r.msg_id} options={ALL_PRAGMATIC} />}
              </div>
              {r.literal_affect && <div className="text-xs text-ink-3 mt-1">字面：{r.literal_affect}</div>}
              {r.method_votes && Object.keys(r.method_votes).length > 0 && (
                <div className="text-xs text-ink-3 mt-1">
                  信号投票：{Object.entries(r.method_votes).map(([k, val]) => `${METHOD_LABELS[k] ?? k}=${val}`).join(" · ")}
                  {(r.similar_examples?.length ?? 0) > 0 && (
                    <> · 最相似例句的标签：{r.similar_examples!.map((s) => `${s.label} ${s.score.toFixed(2)}`).join("，")}</>
                  )}
                </div>
              )}
              {r.tag_basis && tags.some((t) => r.tag_basis?.[t]?.length) && (
                <div className="text-xs text-ink-3 mt-1">
                  {tags.filter((t) => r.tag_basis?.[t]?.length).map((t) => <span key={t} className="mr-2">{t} ← <EvidenceChips ids={r.tag_basis![t]} report={report} onPick={onPick} /></span>)}
                </div>
              )}
              {r.ambiguous && <p className="text-ink-2 mt-1">{r.abstain_reason || "上下文不足以判断。"}</p>}
              {r.suppressed_reason && (
                <p className="text-warn text-xs mt-1">⚠ {r.suppressed_reason}</p>
              )}
              {r.candidates.length > 0 && (
                <ul className="mt-1 text-ink-2">
                  {r.candidates.map((c) => (
                    <li key={c.reading}>· 可能解释：{c.reading}</li>
                  ))}
                </ul>
              )}
              <div className="mt-1 text-xs text-ink-3">
                证据 <EvidenceChips ids={r.evidence_ids} report={report} onPick={onPick} />
                {r.counter_evidence_ids.length > 0 && <> · 反证 <EvidenceChips ids={r.counter_evidence_ids} report={report} onPick={onPick} /></>}
                {r.model_confidence_raw !== null && <> · 模型自评 {r.model_confidence_raw}（未经校准）</>}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function IntentSection({ report, onPick }: { report: Report; onPick: (id: string) => void }) {
  return (
    <Card title="3 · 语义与情绪（逐句）">
      <IntentList report={report} onPick={onPick} />
    </Card>
  );
}
