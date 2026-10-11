import { useState } from "react";
import { Alert, Card, Field } from "@/components/ui";
import { formatDate } from "@/lib/workbench";

export interface InteractionStudy {
  id: string; updated_at?: number; dataset_id: string; limitations?: string[];
  sources: { source_id: string; sessions: number; coverage: { eligible_messages_with_times: number; input_messages: number };
    marker_counts: Record<string, Record<string, number>>; marker_denominators: Record<string, number>;
    change_points: { status: string; stable_indices: number[]; runs: { cost: string; penalty: number; indices: number[] }[] } }[];
}

export function WorkbenchInteractionResearch({ studies }: { studies: InteractionStudy[] }) {
  const [selected, setSelected] = useState("");
  const study = studies.find(item => item.id === selected) ?? studies[0];
  if (!study) return null;
  return <Card title="互动信号与变化点 · 研究结果" hint="复用 ConvoKit 中文标记和 ruptures 离线变化点方法；没有改变关系评分。">
    <div className="space-y-4">
      <Field label="互动研究记录"><select className="input" value={study.id} onChange={event => setSelected(event.target.value)}>{studies.map(item => <option key={item.id} value={item.id}>{formatDate(item.updated_at)} · {item.id.slice(0, 8)}</option>)}</select></Field>
      <Alert>礼貌词不能证明喜欢程度。变化点来自完整历史序列，不是实时预测；没有人工标注的变化点答案，本次不报告准确率。</Alert>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b border-line"><th className="p-2">匿名来源</th><th className="p-2">时间可用的消息</th><th className="p-2">双方会话段</th><th className="p-2">多组参数共同检出的候选位置</th></tr></thead><tbody>{study.sources.map((source, index) => <tr key={source.source_id} className="border-b border-line"><th className="p-2 font-normal">来源 {index + 1} · {source.source_id.slice(0, 8)}</th><td className="p-2">{source.coverage.eligible_messages_with_times} / {source.coverage.input_messages}</td><td className="p-2">{source.sessions}</td><td className="p-2">{source.change_points.status === "insufficient_sessions" ? "会话段不足，暂不判断" : source.change_points.stable_indices.join("、") || "未检出；不等于关系没有变化"}</td></tr>)}</tbody></table></div>
      <details className="text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看双方措辞标记与参数敏感性</summary><p className="mt-2">同一条消息可命中多个标记，计数不能相加当作消息数。下面只有统计和位置，聊天证据保存在本机加密产物中。</p><div className="mt-3 space-y-4">{study.sources.map((source, index) => <section key={source.source_id}><h3 className="font-medium">匿名来源 {index + 1}</h3><div className="overflow-auto"><table className="mt-2 w-full text-left"><thead><tr><th className="p-1">说话方</th><th className="p-1">文本消息</th><th className="p-1">感谢</th><th className="p-1">道歉</th><th className="p-1">礼貌请求</th><th className="p-1">赞美措辞</th></tr></thead><tbody>{["me", "other"].map(who => <tr key={who} className="border-t border-line"><th className="p-1 font-normal">{who === "me" ? "我" : "对方"}</th><td className="p-1">{source.marker_denominators[who] ?? 0}</td>{["gratitude", "apologetic", "please", "praise"].map(marker => <td className="p-1" key={marker}>{source.marker_counts[who]?.[marker] ?? 0}</td>)}</tr>)}</tbody></table></div><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap">{JSON.stringify(source.change_points.runs, null, 2)}</pre></section>)}</div></details>
      {study.limitations?.map(text => <p className="text-xs text-ink-3" key={text}>{text}</p>)}
    </div>
  </Card>;
}
