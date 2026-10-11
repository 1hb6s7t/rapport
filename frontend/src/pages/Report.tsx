import { Fragment, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import type { CallsView, Profile, Report, TrendPoint } from "@/api/types";
import { DimBars, TrendChart } from "@/components/Charts";
import { EvidenceChips, EvidencePanel } from "@/components/Evidence";
import { FeedbackButtons, FeedbackProvider } from "@/components/Feedback";
import { OBSERVED_LABELS, PRIOR_CHECK_LABELS, PRIOR_CHECK_TONES, REACTION_TONES } from "@/i18n/labels";
import type { Option, Situation } from "@/api/types";
import { IntentSection } from "@/components/Intent";
import { Alert, Badge, Card, Empty, ErrorBox, Field, Spinner } from "@/components/ui";
import {
  AXIS_LABELS, BOUNDARY_LABELS, CHAIN_LABELS, CHAIN_TONES, CHINESE_ZODIAC, CONF_LABELS, DIM_HINT, DIM_LABELS, DIM_ORDER,
  MBTI_TYPES, MODE_LABELS, MOMENTUM_LABELS, QUALITY_FLAGS, SCRIPT_LABELS, STAGE_LABELS, TENDENCY_LABELS, VERDICT_LABELS, ZODIAC,
} from "@/i18n/labels";
import { useFlow } from "@/store";

/** One line of "what the rules actually read" for a trend segment: metric names and values, with
 *  unavailable metrics named rather than shown as zero. */
function inputsSummary(inp?: Record<string, { value: number | null; n: number; unavailable_reason?: string | null }>): string {
  if (!inp) return "—";
  const parts = Object.entries(inp)
    .slice(0, 4)
    .map(([k, v]) => (v.value === null ? `${k} 不可用(${v.unavailable_reason ?? "无数据"})` : `${k} ${v.value}`));
  return parts.join(" · ") || "—";
}

export function ReportPage() {
  const { reportId: paramId } = useParams();
  const nav = useNavigate();
  const [flow, setFlow] = useFlow();
  const reportId = paramId ?? flow.reportId;
  const [r, setR] = useState<Report | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [pick, setPick] = useState<string | null>(null);
  const [dim, setDim] = useState<string | null>(null);
  const [point, setPoint] = useState<TrendPoint | null>(null);
  const [withEvidence, setWithEvidence] = useState(false);

  useEffect(() => {
    if (!reportId) return;
    // switching reports must not show the previous one (or its error) while the next loads
    setR(null);
    setErr(null);
    setPick(null);
    setDim(null);
    setPoint(null);
    let alive = true;
    const p = reportId === "demo" ? api.get<Report>("/demo") : api.get<Report>(`/reports/${reportId}`);
    p.then((v) => alive && setR(v)).catch((e) => alive && setErr(e));
    return () => {
      alive = false;
    };
  }, [reportId]);

  if (!reportId) return <Alert tone="info">还没有报告。<a href="/import">从导入开始</a>，或<a href="/report/demo" className="ml-1">看演示报告</a>。</Alert>;
  if (err) return <ErrorBox error={err} />;
  if (!r) return <Spinner label="读取报告…" />;
  const a = r.affinity;
  const isDemo = r.mode === "demo";

  async function exportAs(fmt: "md" | "json") {
    const res = await fetch(`/api/reports/${r!.id}/export?format=${fmt}&include_evidence=${withEvidence}`, { credentials: "same-origin" });
    if (!res.ok) {
      setErr(new Error(`导出失败（${res.status}）`));
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const el = document.createElement("a");
    el.href = url;
    el.download = `rapport-${r!.id}.${fmt}`;
    el.click();
    URL.revokeObjectURL(url);
  }

  return (
    <FeedbackProvider reportId={r.id} disabled={isDemo}>
    <div className="space-y-4 pb-24">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">关系复盘报告 <span className="mono text-sm text-ink-3">{r.id}</span></h1>
          <div className="flex flex-wrap gap-2 mt-1 text-sm">
            <Badge tone={isDemo ? "accent" : r.mode === "byok" ? "info" : "neutral"}>{MODE_LABELS[r.mode]}</Badge>
            {r.synthetic && <Badge tone="accent">合成演示数据</Badge>}
            {r.ai_status === "failed" && <Badge tone="warn">AI 部分失败，已回退规则</Badge>}
            {r.ai_status === "insufficient" && <Badge tone="warn">材料不足，AI 未生成结论</Badge>}
            {r.scope.quality_flags.map((f) => <Badge key={f}>{QUALITY_FLAGS[f] ?? f}</Badge>)}
          </div>
        </div>
        {!isDemo && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <label className="flex items-center gap-1 text-xs text-ink-3"><input type="checkbox" checked={withEvidence} onChange={(e) => setWithEvidence(e.target.checked)} />导出含脱敏证据片段</label>
            <button className="btn btn-ghost py-1" onClick={() => exportAs("md")}>导出 Markdown</button>
            <button className="btn btn-ghost py-1" onClick={() => exportAs("json")}>导出 JSON</button>
            <button className="btn btn-danger py-1" onClick={async () => { if (confirm("删除这份报告？")) { await api.del(`/reports/${r.id}`); setFlow({ reportId: null }); nav("/settings"); } }}>删除</button>
          </div>
        )}
      </header>

      {r.notes.map((n) => <Alert key={n} tone="info">{n}</Alert>)}
      {r.ai_error && <Alert tone="warn" title="AI 错误">{r.ai_error}</Alert>}

      {/* 1. scope */}
      <Card title="1 · 分析范围与材料质量">
        <dl className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
          <Stat k="纳入消息" v={`${r.scope.msg_analyzed} / ${r.scope.msg_total}`} />
          <Stat k="对方 / 我方文本" v={`${r.scope.text_other} / ${r.scope.text_me}`} />
          <Stat k="会话段" v={String(r.scope.sessions)} />
          <Stat k="时间范围" v={r.scope.range_start && r.scope.range_end ? `${r.scope.range_start.slice(0, 10)} ~ ${r.scope.range_end.slice(0, 10)}` : "无可靠时间"} />
          <Stat k="观察覆盖度" v={a.coverage.toFixed(2)} />
        </dl>
      </Card>

      {r.boundary.value !== "none" && (
        <Alert tone="warn" title={`边界信号：${BOUNDARY_LABELS[r.boundary.value]}`}>
          {r.boundary.note} 这只是提示，不改变任何分数；证据：<EvidenceChips ids={r.boundary.basis_ids} report={r} onPick={setPick} />
        </Alert>
      )}
      {/* 2. affinity */}
      <Card title="2 · 好感度指数" right={<Badge tone="neutral">{MOMENTUM_LABELS[r.trend.momentum]}</Badge>}>
        <div className="grid gap-5 md:grid-cols-[220px_1fr]">
          <div className="text-center md:text-left">
            {a.total !== null ? (
              <>
                <div className="text-6xl font-semibold tracking-tight" style={{ background: "linear-gradient(135deg, var(--color-brand), var(--color-accent))", WebkitBackgroundClip: "text", color: "transparent" }}>{a.total}</div>
                <div className="text-xs text-ink-3 flex flex-wrap items-center gap-1 justify-center md:justify-start">/ 100 · 基于可观察互动行为 <Badge tone={a.confidence === "high" ? "good" : a.confidence === "low" ? "warn" : "neutral"}>置信度 {CONF_LABELS[a.confidence ?? "medium"] ?? a.confidence}</Badge> <FeedbackButtons kind="affinity" target="total" /></div>
              </>
            ) : (
              <div className="rounded-lg bg-paper p-3 text-sm"><strong>没有可用维度</strong><br /><span className="text-ink-2">材料里没有任何能计算的指标</span></div>
            )}
            {(a.caveats?.length ?? 0) > 0 && (
              <ul className="mt-2 text-xs text-ink-2 space-y-0.5 text-left">
                {a.caveats!.map((cv) => <li key={cv}>⚠ {cv}</li>)}
              </ul>
            )}
            <p className="text-xs text-ink-3 mt-3 leading-relaxed">{a.disclaimer}</p>
          </div>
          <div>
            <DimBars dims={a.dims} onPick={setDim} />
            {dim && a.dims[dim] && (
              <div className="rounded-lg bg-paper p-3 text-sm mt-2">
                <div className="flex flex-wrap items-center gap-2">
                  <strong>{DIM_LABELS[dim]}</strong>
                  <span className="text-ink-3 text-xs">{DIM_HINT[dim]}</span>
                  <Badge>置信度 {CONF_LABELS[a.dims[dim].confidence]}</Badge>
                  <span className="text-xs text-ink-3">规则 {a.dims[dim].rule ?? "—"} · AI {a.dims[dim].llm ?? "—"}</span>
                </div>
                <p className="mt-1 text-ink-2">{a.dims[dim].note}</p>
                <EvidenceChips ids={a.dims[dim].evidence_ids} report={r} onPick={setPick} />
                {dim === "future" && a.future_chains?.length > 0 && (
                  <div className="mt-2 border-t border-line pt-2">
                    <div className="text-xs text-ink-3 mb-1">
                      邀约链（{a.future_chains.length} 条）：我说的话 → 对方的回应
                    </div>
                    <ul className="space-y-1 text-xs">
                      {a.future_chains.slice(0, 8).map((c) => (
                        <li key={c.propose_id} className="flex flex-wrap items-center gap-1.5">
                          <button
                            type="button"
                            className="underline decoration-dotted hover:text-ink"
                            onClick={() => setPick(c.propose_id)}
                          >
                            「{c.propose_text}」
                          </button>
                          <span className="text-ink-3">→</span>
                          <Badge tone={CHAIN_TONES[c.response]}>{CHAIN_LABELS[c.response] ?? c.response}</Badge>
                          {c.commit_ids.length > 0 && <span className="text-ink-3">谈定了具体时间</span>}
                        </li>
                      ))}
                    </ul>
                    {a.future_chains.length > 8 && (
                      <div className="text-xs text-ink-3 mt-1">只列出前 8 条；其余按同样口径计入分数。</div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
        <div className="mt-5">
          <h3 className="text-sm font-medium mb-1">变化趋势 <span className="text-ink-3 font-normal">（{r.trend.note}）</span></h3>
          <TrendChart points={r.trend.points} momentum={r.trend.momentum} onPick={setPoint} />
          {(r.trend.change_points?.length ?? 0) > 0 && (
            <div className="mt-2 rounded-lg border border-line p-3 text-xs">
              <div className="font-medium">探索性变化点 <span className="text-ink-3 font-normal">（ruptures PELT，会话特征序列）</span></div>
              <ul className="mt-1 space-y-1">
                {r.trend.change_points!.map((c) => (
                  <li key={c.index} className="flex flex-wrap items-center gap-2">
                    <span>{c.date ? new Date(c.date).toLocaleDateString() : `第 ${c.index + 1} 段会话`}</span>
                    <Badge tone={c.stability >= 0.75 ? "neutral" : "warn"}>参数一致度 {Math.round(c.stability * 100)}%</Badge>
                    <span className="text-ink-3">{c.note}</span>
                    <EvidenceChips ids={c.evidence_ids} report={r} onPick={setPick} />
                  </li>
                ))}
              </ul>
              {r.trend.change_note && <p className="mt-1 text-ink-3">{r.trend.change_note}</p>}
            </div>
          )}
          {point && (
            <div className="rounded-lg bg-paper p-3 text-sm mt-2">
              <div className="flex flex-wrap items-center gap-2">
                <strong>{point.label}</strong>
                <span className="text-xs text-ink-3">
                  消息 {point.n_msgs} 条 · 对方文本 {point.n_other_text} 条
                  {point.insufficient ? " · 样本不足，这一段不计分" : ""}
                </span>
                <button type="button" className="ml-auto text-xs underline decoration-dotted" onClick={() => setPoint(null)}>
                  收起
                </button>
              </div>
              <table className="mt-2 w-full text-xs">
                <thead>
                  <tr className="text-left text-ink-3">
                    <th className="font-normal">维度</th>
                    <th className="font-normal">这段的分</th>
                    <th className="font-normal">规则读到的输入</th>
                    <th className="font-normal">模型覆盖</th>
                    <th className="font-normal">证据</th>
                  </tr>
                </thead>
                <tbody>
                  {DIM_ORDER.map((k) => (
                    <tr key={k} className="align-top">
                      <td className="pr-2">{DIM_LABELS[k]}</td>
                      <td className="pr-2">{point.dims[k] ?? "—"}</td>
                      <td className="pr-2 text-ink-3">{inputsSummary(point.dim_inputs?.[k])}</td>
                      <td className="pr-2">{point.llm_override?.[k] ?? "—"}</td>
                      <td>
                        <EvidenceChips ids={point.dim_evidence?.[k] ?? []} report={r} onPick={setPick} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(point.evidence_ids?.length ?? 0) > 0 && (
                <div className="mt-2 text-xs text-ink-3">
                  这一段用到的消息：<EvidenceChips ids={point.evidence_ids ?? []} report={r} onPick={setPick} />
                </div>
              )}
              <p className="mt-2 text-xs text-ink-3">
                点某一条消息可以看到原文；「模型覆盖」是这一段模型抽取给出的值（与规则各半加权后进入分数），空着表示这一段只用了规则。
              </p>
            </div>
          )}
        </div>
      </Card>

      <IntentSection report={r} onPick={setPick} />

      {/* 4. observations */}
      <Card title="4 · 观察与证据">
        {r.observations.length === 0 ? (
          <p className="text-sm text-ink-3">规则模式不生成语义观察；开启 AI 分析后这里会列出带证据与反证的观察。</p>
        ) : (
          <ul className="space-y-3">
            {r.observations.map((o) => (
              <li key={o.id} className="text-sm">
                <div><strong>{o.claim}</strong><EvidenceChips ids={o.evidence_ids} report={r} onPick={setPick} /><FeedbackButtons kind="observation" target={o.id} /></div>
                {o.counter_evidence_ids.length > 0 && <div className="text-ink-2 mt-0.5">反证：<EvidenceChips ids={o.counter_evidence_ids} report={r} onPick={setPick} /></div>}
                {o.alternatives.map((alt) => <div key={alt} className="text-ink-3 mt-0.5">· 其它解释：{alt}</div>)}
                {o.limitations && <div className="text-ink-3 mt-0.5">· 局限：{o.limitations}</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* 5. prior */}
      {(r.priors.length > 0 || r.prior_breakdown.alpha > 0) && (
        <Card title="5 · 标签先验（可被推翻的起始假设）">
          <p className="text-xs text-ink-3 mb-2">
            这些是<strong>假设</strong>而不是结论：不参与好感度分数，只作初识阶段的起步参考，会随真实聊天证据增加而衰减。
            本报告实际标签权重合计 {Object.values(r.prior_breakdown.weights).reduce((a, b) => a + b, 0).toFixed(2)}，其余 {r.prior_breakdown.unknown.toFixed(2)} 归聊天证据与「未知」；有效证据量 {r.prior_breakdown.n_eff}；本次标签预算 {Math.round((r.prior_breakdown.alpha0 ?? 0.15) * 100)}%（设置 → 个性化权重可调）。{(r.prior_breakdown.evidence_counts?.media_unobserved ?? 0) > 0 && <>另有 {r.prior_breakdown.evidence_counts!.media_unobserved} 条图片/语音/视频/表情未读取内容，不计入证据。</>}
          </p>
          {r.priors.length === 0 ? <Empty>没有填写任何标签，因此没有先验假设</Empty> : (
            <ul className="text-sm space-y-2">
              {r.priors.map((p) => (
                <li key={p.label}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="accent">{p.label === "mbti" ? "MBTI" : p.label === "zodiac" ? "星座" : "属相"} {p.value}</Badge>
                    <span className="text-xs text-ink-3">权重 {p.weight.toFixed(3)}</span>
                    {p.entertainment && <Badge>娱乐性</Badge>}
                  </div>
                  <p className="text-ink-2 mt-0.5">{p.hypothesis}</p>
                  <p className="text-xs text-ink-3">{p.basis}</p>
                </li>
              ))}
            </ul>
          )}
          {r.prior_checks.length > 0 && (
            <div className="mt-3">
              <h3 className="text-sm font-medium mb-1">起始假设 vs 聊天里观察到的</h3>
              <ul className="text-sm space-y-1">
                {r.prior_checks.map((pc) => (
                  <li key={pc.tendency_key} className="flex flex-wrap items-center gap-2">
                    <span className="text-ink-3 text-xs w-20">{TENDENCY_LABELS[pc.tendency_key] ?? pc.tendency_key}</span>
                    <span>假设 <strong>{pc.lean_label}</strong></span>
                    <Badge tone={PRIOR_CHECK_TONES[pc.status]}>{PRIOR_CHECK_LABELS[pc.status]}</Badge>
                    {pc.status !== "untested" && pc.observed_label && <span className="text-ink-2">观察到「{pc.observed_label}」</span>}
                    {pc.evidence_ids.length > 0 && <EvidenceChips ids={pc.evidence_ids} report={r} onPick={setPick} />}
                    {pc.status === "untested" && <span className="text-xs text-ink-3">会推翻它的迹象：{pc.counterexample_prompt}</span>}
                    {pc.status === "contradicted" && <FeedbackButtons kind="trait" target={`prior:${pc.tendency_key}`} />}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {Object.values(r.prior_breakdown.weights).reduce((a, b) => a + b, 0) < .02 && r.priors.length > 0 && <Alert tone="info">本报告中的标签参考权重已低于 2%；未知仍然保留，不视为已证实的性格结论。</Alert>}
        </Card>
      )}

      {/* 6. persona */}
      <Card title="6 · 对方的沟通侧写">
        {!r.persona.available ? (
          <p className="text-sm text-ink-3">信息不足或未开启 AI 分析。侧写需要足够的对方文本与 AI 综合。</p>
        ) : (
          <div className="space-y-4">
            <p className="text-xs text-ink-3">{r.persona.limitations}</p>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(TENDENCY_LABELS).map(([k, label]) => {
                const t = r.persona.tendencies[k];
                return (
                  <div key={k} className="rounded-lg border border-line p-2 text-sm">
                    <div className="text-xs text-ink-3">{label}</div>
                    <div className="font-medium">{t?.label ?? "信息不足"}</div>
                    {t && r.persona.pole_stability?.[k] != null && <div className="text-xs text-ink-3">最近版本一致 {Math.round((r.persona.pole_stability[k] ?? 0) * 100)}%</div>}
                    {t && <EvidenceChips ids={t.evidence_ids} report={r} onPick={setPick} />}
                  </div>
                );
              })}
            </div>
            {r.persona.traits.length > 0 && (
              <ul className="text-sm space-y-1">
                {r.persona.traits.map((t) => <li key={t.statement}>· {t.statement} <Badge>{CONF_LABELS[t.confidence]}</Badge><EvidenceChips ids={t.evidence_ids} report={r} onPick={setPick} /><FeedbackButtons kind="trait" target={t.statement} /></li>)}
              </ul>
            )}
            <div>
              <h3 className="text-sm font-medium mb-1">四轴倾向（仅反映本段材料）</h3>
              <div className="grid gap-2 sm:grid-cols-4 text-sm">
                {Object.entries(AXIS_LABELS).map(([k, label]) => {
                  const ax = r.persona.inferred_axes[k];
                  return (
                    <div key={k} className="rounded-lg bg-paper p-2">
                      <div className="text-xs text-ink-3">{label}</div>
                      <div className="font-medium">{ax?.lean === "unclear" || !ax ? "不明确" : `偏 ${ax.lean}`} <span className="text-xs text-ink-3">{ax ? CONF_LABELS[ax.confidence] : ""}</span></div>
                      {ax?.note && <div className="text-xs text-ink-3">{ax.note}</div>}
                    </div>
                  );
                })}
              </div>
            </div>
            <MbtiCompare r={r} onUpdated={setR} isDemo={isDemo} />
          </div>
        )}
      </Card>

      {/* 5. stage & boundary */}
      <Card title="7 · 关系阶段与边界">
        <div className="grid gap-3 md:grid-cols-2 text-sm">
          <div className="rounded-lg border border-line p-3">
            <div className="text-xs text-ink-3">阶段（描述，不是等级）</div>
            <div className="font-medium text-lg">{STAGE_LABELS[r.stage.value] ?? r.stage.value}{r.stage.user_confirmed && <Badge tone="info">用户确认</Badge>}<FeedbackButtons kind="stage" target={r.stage.value} /></div>
            {r.stage.change && (
              <p className="text-xs text-ink-3">
                较上次：{STAGE_LABELS[r.stage.change.before] ?? r.stage.change.before} → {STAGE_LABELS[r.stage.change.after] ?? r.stage.change.after}
                （{{ advance: "推进", regress: "回退", same: "未变" }[r.stage.change.direction] ?? r.stage.change.direction}）：{r.stage.change.reason}
              </p>
            )}
            <p className="text-ink-2">{r.stage.note}</p>
            <EvidenceChips ids={r.stage.basis_ids} report={r} onPick={setPick} />
          </div>
          <div className={`rounded-lg border p-3 ${r.boundary.value !== "none" ? "border-warn bg-warn-soft/40" : "border-line"}`}>
            <div className="text-xs text-ink-3">边界（优先于一切分数）</div>
            <div className="font-medium text-lg">{r.boundary.value !== "none" && <span aria-hidden>⚠ </span>}{BOUNDARY_LABELS[r.boundary.value]}<FeedbackButtons kind="boundary" target={r.boundary.value} /></div>
            <p className="text-ink-2">{r.boundary.note}</p>
            <EvidenceChips ids={r.boundary.basis_ids} report={r} onPick={setPick} />
          </div>
        </div>
      </Card>

      {/* 6. playbook */}
      <Card title="8 · 攻略：下一步怎么做">
        {r.playbook.change_notes.length > 0 && (
          <div className="rounded-lg bg-accent-soft/40 p-3 mb-3 text-sm">
            <div className="font-medium mb-1">与上次相比的变化</div>
            <ul className="space-y-0.5">
              {r.playbook.change_notes.map((n) => <li key={n}>· {n}</li>)}
            </ul>
          </div>
        )}
        {r.playbook.note && <p className="text-xs text-ink-3 mb-3">{r.playbook.note}</p>}
        {r.playbook.done_review.length > 0 && (
          <div className="rounded-lg border border-line bg-paper p-3 mb-3 text-sm">
            <div className="font-medium mb-1">上次「我做了」的回顾</div>
            <ul className="space-y-1">
              {r.playbook.done_review.map((d) => (
                <li key={d.option_id + d.goal}>· {d.goal}：对方<strong>{OBSERVED_LABELS[d.observed] ?? d.observed}</strong>。<span className="text-ink-2">{d.verdict}</span>{d.note && <span className="text-ink-3">（{d.note}）</span>}</li>
              ))}
            </ul>
          </div>
        )}
        {r.playbook.situations.length > 0 && (
          <div className="space-y-3 mb-4">
            {r.playbook.situations.map((s) => <SituationCard key={s.id} s={s} r={r} reportId={r.id} isDemo={isDemo} onPick={setPick} />)}
          </div>
        )}
        <div className="space-y-3">
          {r.playbook.situations.length === 0 && r.playbook.actions.map((act, i) => (
            <div key={i} className="rounded-lg border border-line p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2"><strong>行动 {i + 1}：{act.goal}</strong>{!act.personalized && <Badge>通用骨架</Badge>}<FeedbackButtons kind="action" target={String(i)} /></div>
              <p className="mt-1">{act.action}<EvidenceChips ids={act.basis_ids} report={r} onPick={setPick} /></p>
              {act.suggested_text && <Copyable label="示例（可编辑后自己发送；本工具不会代发）" text={act.suggested_text} />}
              {act.observe && <p className="text-ink-2 mt-1">观察：{act.observe}</p>}
              <p className="mt-1"><Badge tone="warn">停止条件</Badge> <span className="text-ink-2">{act.stop_when}</span></p>
              {act.caution && <p className="mt-1 text-warn text-xs">⚠ {act.caution}</p>}
              {act.alternatives.map((alt) => <p key={alt} className="text-ink-3 mt-0.5">备选：{alt}</p>)}
            </div>
          ))}
        </div>
        {r.playbook.landmines.length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-medium mb-1">雷区</h3>
            <ul className="text-sm space-y-1">
              {r.playbook.landmines.map((l) => <li key={l.topic}>⛔ <strong>{l.topic}</strong> — {l.why}<EvidenceChips ids={l.evidence_ids} report={r} onPick={setPick} /><FeedbackButtons kind="landmine" target={l.topic} /></li>)}
            </ul>
          </div>
        )}
        {Object.keys(r.playbook.scripts).length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-medium mb-1">话术模板（示例，不保证结果）</h3>
            <div className="grid gap-2 md:grid-cols-3">
              {Object.entries(r.playbook.scripts).map(([k, s]) => (
                <div key={k} className="rounded-lg bg-paper p-3 text-sm">
                  <div className="text-xs text-ink-3 mb-1">{SCRIPT_LABELS[k] ?? k} · {s.when_to_use}{s.caution && <span className="text-warn"> · ⚠ {s.caution}</span>}</div>
                  <Copyable text={s.text} />
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* 7. fun */}
      <details className="card">
        <summary className="cursor-pointer text-base font-semibold">9 · 娱乐彩蛋（星座 / 属相，权重 0）</summary>
        <FunSection r={r} onUpdated={setR} isDemo={isDemo} />
      </details>

      {/* 8. meta */}
      <Card title="10 · 版本与费用">
        <dl className="text-xs grid grid-cols-2 md:grid-cols-4 gap-2 text-ink-2">
          <Stat k="应用" v={r.versions.app} /><Stat k="评分协议" v={r.versions.rubric} /><Stat k="特征" v={r.versions.features} />
          <Stat k="模型" v={r.versions.model ? `${r.versions.provider} / ${r.versions.model}` : "未使用"} />
          {r.cost.actual_usage && <Stat k="实际用量" v={`${r.cost.actual_usage.calls} 次 · ${r.cost.actual_usage.prompt_tokens}/${r.cost.actual_usage.completion_tokens} tokens${r.cost.actual_cny !== null ? ` · ≈¥${r.cost.actual_cny}` : ""}`} />}
          <Stat k="生成时间" v={new Date(r.created_at).toLocaleString()} />
        </dl>
        {r.mode === "byok" && !isDemo && <CallLedger />}
      </Card>

      <EvidencePanel id={pick} report={r} onClose={() => setPick(null)} />
    </div>
    </FeedbackProvider>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-3">{k}</dt>
      <dd className="font-medium break-words">{v}</dd>
    </div>
  );
}

function SituationCard({ s, r, reportId, isDemo, onPick }: { s: Situation; r: Report; reportId: string; isDemo: boolean; onPick: (id: string) => void }) {
  const [open, setOpen] = useState(0);
  return (
    <div className="rounded-lg border border-line p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <strong>情境：{s.title}</strong>
        <EvidenceChips ids={s.trigger_ids} report={r} onPick={onPick} />
      </div>
      {s.why && <p className="text-ink-2 mt-1">{s.why}</p>}
      <div className="mt-2 flex flex-wrap gap-1" role="tablist" aria-label="选项">
        {s.options.map((o, i) => (
          <button key={o.id} role="tab" aria-selected={open === i} className={`px-3 py-1 rounded-md text-xs border ${open === i ? "bg-ink text-white border-ink" : "border-line hover:bg-paper"}`} onClick={() => setOpen(i)}>
            选项 {i + 1}
          </button>
        ))}
      </div>
      {s.options[open] && <OptionBody o={s.options[open]} r={r} reportId={reportId} isDemo={isDemo} onPick={onPick} />}
    </div>
  );
}

function OptionBody({ o, r, reportId, isDemo, onPick }: { o: Option; r: Report; reportId: string; isDemo: boolean; onPick: (id: string) => void }) {
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mark = async (observed: string) => {
    setBusy(true);
    try {
      await api.post(`/reports/${reportId}/actions/${o.id}/done`, { goal: o.goal, observed });
      setDone(observed);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-2 space-y-1">
      <div className="flex flex-wrap items-center gap-2"><strong>{o.goal}</strong>{!o.personalized && <Badge>通用骨架</Badge>}<FeedbackButtons kind="action" target={o.id} /></div>
      <p>{o.action}<EvidenceChips ids={o.trigger_ids} report={r} onPick={onPick} /></p>
      {o.suggested_text && <Copyable label="示例（可编辑后自己发送；本工具不会代发）" text={o.suggested_text} />}
      <ul className="text-ink-2">
        {o.expected_reactions.map((x, i) => (
          <li key={i}><Badge tone={REACTION_TONES[x.signal] ?? "neutral"}>若对方{x.if_they}</Badge> <span className="ml-1">{x.then}</span></li>
        ))}
      </ul>
      <p><Badge tone="warn">停止条件</Badge> <span className="text-ink-2">{o.stop_when}</span></p>
      {o.risk && <p className="text-ink-3">风险：{o.risk}</p>}
      {o.caution && <p className="text-warn text-xs">⚠ {o.caution}</p>}
      {!isDemo && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs">
          <span className="text-ink-3 mr-1">我做了，对方：</span>
          {done ? (
            <Badge tone="good">已记录：{OBSERVED_LABELS[done] ?? done}</Badge>
          ) : (
            Object.entries(OBSERVED_LABELS).map(([k, label]) => (
              <button key={k} type="button" disabled={busy} className="px-2 py-0.5 rounded border border-line hover:bg-paper disabled:opacity-50" onClick={() => mark(k)}>{label}</button>
            ))
          )}
          <span className="text-ink-3 ml-1">只存本机，下次分析会回顾；拒绝过的目标不再推荐。</span>
        </div>
      )}
    </div>
  );
}

function Copyable({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="mt-1">
      {label && <div className="text-xs text-ink-3">{label}</div>}
      <div className="flex items-start gap-2">
        <blockquote className="flex-1 border-l-2 border-accent pl-2 text-ink whitespace-pre-wrap">{text}</blockquote>
        <button className="btn btn-ghost py-0.5 text-xs" onClick={() => navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>{done ? "已复制" : "复制"}</button>
      </div>
    </div>
  );
}

function useProfileEditor(r: Report, onUpdated: (r: Report) => void, isDemo: boolean) {
  const [flow] = useFlow();
  const [p, setP] = useState<Profile>(flow.profile);
  const [busy, setBusy] = useState(false);
  async function apply(next: Profile) {
    setP(next);
    if (isDemo) return;
    setBusy(true);
    try {
      onUpdated(await api.patch<Report>(`/reports/${r.id}/profile`, { profile: next }));
    } finally {
      setBusy(false);
    }
  }
  return { p, apply, busy };
}

function MbtiCompare({ r, onUpdated, isDemo }: { r: Report; onUpdated: (r: Report) => void; isDemo: boolean }) {
  const { p, apply, busy } = useProfileEditor(r, onUpdated, isDemo);
  const c = r.persona.mbti_comparison;
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-medium">与对方自述 MBTI 对照</h3>
        <select className="input py-1 w-auto text-sm" value={p.other.mbti ?? ""} disabled={busy || isDemo} onChange={(e) => apply({ ...p, other: { ...p.other, mbti: e.target.value || null } })} aria-label="对方 MBTI">
          <option value="">未填写</option>
          {MBTI_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <span className="text-xs text-ink-3">改标签不会改变任何分数或建议</span>
      </div>
      {c ? (
        <div className="text-sm mt-2">
          <div><Badge tone={c.verdict === "similar" ? "good" : c.verdict === "context_differs" ? "accent" : "neutral"}>{VERDICT_LABELS[c.verdict]}</Badge> <span className="text-ink-3">自述 {c.declared} · 聊天推断 {c.inferred}</span></div>
          <ul className="mt-1 text-ink-2 space-y-0.5">{c.notes.map((n) => <li key={n}>· {n}</li>)}</ul>
        </div>
      ) : (
        <p className="text-sm text-ink-3 mt-2">填写对方 MBTI 后显示对照。</p>
      )}
    </div>
  );
}

function FunSection({ r, onUpdated, isDemo }: { r: Report; onUpdated: (r: Report) => void; isDemo: boolean }) {
  const { p, apply, busy } = useProfileEditor(r, onUpdated, isDemo);
  return (
    <div className="mt-3 space-y-3 text-sm">
      <p className="text-xs text-ink-3">{r.fun.disclaimer}</p>
      {!isDemo && (
        <div className="grid gap-2 grid-cols-2 md:grid-cols-4">
          {(["me", "other"] as const).map((who) => (
            <Fragment key={who}>
              <Field key={who + "z"} label={`${who === "me" ? "我" : "对方"}的星座`}>
                <select className="input py-1" disabled={busy} value={p[who].zodiac ?? ""} onChange={(e) => apply({ ...p, [who]: { ...p[who], zodiac: e.target.value || null } })}>
                  <option value="">不填</option>{ZODIAC.map(([v, l]) => <option key={v} value={v}>{l}座</option>)}
                </select>
              </Field>
              <Field key={who + "c"} label={`${who === "me" ? "我" : "对方"}的属相`}>
                <select className="input py-1" disabled={busy} value={p[who].chinese_zodiac ?? ""} onChange={(e) => apply({ ...p, [who]: { ...p[who], chinese_zodiac: e.target.value || null } })}>
                  <option value="">不填</option>{CHINESE_ZODIAC.map(([v, l]) => <option key={v} value={v}>属{l}</option>)}
                </select>
              </Field>
            </Fragment>
          ))}
        </div>
      )}
      {r.fun.zodiac_pair && <p>✨ <strong>{r.fun.zodiac_pair.title}</strong>：{r.fun.zodiac_pair.text}</p>}
      {r.fun.chinese_zodiac_pair && <p>🧧 <strong>{r.fun.chinese_zodiac_pair.title}</strong>：{r.fun.chinese_zodiac_pair.text}</p>}
      {!r.fun.zodiac_pair && !r.fun.chinese_zodiac_pair && <p className="text-ink-3">填写双方星座或属相后显示。</p>}
    </div>
  );
}

function CallLedger() {
  const [flow] = useFlow();
  const [calls, setCalls] = useState<CallsView | null>(null);
  const [open, setOpen] = useState(false);
  const analysisId = flow.analysisId;
  useEffect(() => {
    if (!open || !analysisId || calls) return;
    api.get<CallsView>(`/analyses/${analysisId}/calls`).then(setCalls).catch(() => setCalls(null));
  }, [open, analysisId, calls]);
  if (!analysisId) return null;
  return (
    <div className="mt-3 text-xs">
      <button className="text-ink-3 underline" onClick={() => setOpen((v) => !v)} aria-expanded={open}>{open ? "收起模型调用明细" : "查看模型调用明细（不含任何正文）"}</button>
      {open && calls && (
        <div className="mt-2">
          <p className="text-ink-2">共 {calls.summary.calls} 次调用 · 输入 {calls.summary.prompt_tokens} / 输出 {calls.summary.completion_tokens} tokens{calls.summary.truncated > 0 && ` · ${calls.summary.truncated} 次被截断`}{calls.summary.failed > 0 && ` · ${calls.summary.failed} 次失败`}{calls.summary.cost_cny !== null && ` · ≈¥${calls.summary.cost_cny}`}</p>
          <table className="w-full mt-1">
            <thead className="text-ink-3 text-left"><tr><th>用途</th><th>输入</th><th>输出</th><th>预算</th><th>耗时</th><th>状态</th></tr></thead>
            <tbody>
              {calls.calls.map((c) => (
                <tr key={c.id} className="border-t border-line">
                  <td>{c.purpose}</td><td>{c.prompt_tokens}</td><td>{c.completion_tokens}</td><td>{c.max_tokens}</td><td>{(c.latency_ms / 1000).toFixed(1)}s</td>
                  <td className={c.status === "ok" ? "text-good" : "text-warn"}>{c.status}{c.error_code ? ` (${c.error_code})` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && !calls && <p className="text-ink-3 mt-1">没有找到这次分析的调用记录。</p>}
    </div>
  );
}
