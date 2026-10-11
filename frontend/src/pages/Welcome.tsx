/** 首页 = 关系仪表盘（docs/DESIGN.md §30）。
 *  时间线 → 先验 → 行动 → 隐私。每个数字都能点回证据。 */

import { lazy, Suspense, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, FileUp, Sparkles } from "lucide-react";
import { api, getCsrf } from "@/api/client";
import type { RelationshipOverview, Report } from "@/api/types";
import { useHealth } from "@/components/Layout";
import {
  Badge,
  Button,
  Card,
  ChineseZodiacOrbit,
  ConfidenceBadge,
  EmptyState,
  EvidenceCard,
  InsightTag,
  MetricCard,
  PersonalityCompass,
  PrivacyBadge,
  ProgressRing,
  RelationshipTimeline,
  SectionTitle,
  Spinner,
  ZodiacConstellation,
  type TimelineNode,
} from "@/components/ui";
import { EvidenceChips, EvidencePanel } from "@/components/Evidence";
import { CHINESE_ZODIAC, CONF_LABELS, DIM_LABELS, DIM_ORDER, PRIOR_CHECK_LABELS, STAGE_LABELS, ZODIAC } from "@/i18n/labels";
import { useFlow } from "@/store";
import { WatercolorOrbit } from "@/components/brand/WatercolorOrbit";

const TrendChart = lazy(() => import("@/components/Charts").then(module => ({ default: module.TrendChart })));

interface ReportListItem { id: string; created_at: string; mode: string; total: number | null; relationship_id: string }

const ZODIAC_MAP = Object.fromEntries(ZODIAC);
const ZODIAC_ELEMENT: Record<string, string> = {
  aries: "火", leo: "火", sagittarius: "火", taurus: "土", virgo: "土", capricorn: "土",
  gemini: "风", libra: "风", aquarius: "风", cancer: "水", scorpio: "水", pisces: "水",
};
const CZ_MAP = Object.fromEntries(CHINESE_ZODIAC);


function Hero({ name, hasData }: { name?: string | null; hasData: boolean }) {
  const nav = useNavigate();
  return (
    <section className="workspace-welcome-hero relative overflow-hidden card card-lg anim-fade-up">
      <div className="grid items-center gap-6 md:grid-cols-[1fr_auto]">
        <div>
          <p className="text-[11px] uppercase tracking-[0.22em] text-brand-ink">心迹 · RAPPORT</p>
          <h1 className="mt-2 text-2xl md:text-[26px] font-semibold leading-snug">
            {hasData ? `与${name || "对方"}的对话里，有一些值得注意的变化。` : "把一段对话，复盘成看得见证据的判断。"}
          </h1>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink-2">
            基于真实聊天证据整理，而不是猜测对方的想法。每一条结论都能点回原文，每一处推断都标明不确定。
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="primary" onClick={() => nav("/import")}>
              <FileUp size={15} strokeWidth={1.8} /> 导入对话
            </Button>
            <Button variant="ghost" onClick={() => nav("/report/demo")}>
              <Sparkles size={15} strokeWidth={1.8} /> 看演示报告
            </Button>
          </div>
        </div>
        <div className="workspace-orbit hidden sm:block"><WatercolorOrbit compact /></div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- 各模块 */

function Pulse({ r, overview }: { r: Report; overview: RelationshipOverview | null }) {
  const nav = useNavigate();
  const a = r.affinity;
  const pts = (overview?.trend.points ?? []).filter((p) => p.total != null);
  const delta = pts.length >= 2 ? (pts[pts.length - 1].total ?? 0) - (pts[pts.length - 2].total ?? 0) : null;
  return (
    <Card large title={<span className="flex items-center gap-2">关系脉搏</span>} right={a.confidence ? <ConfidenceBadge level={a.confidence} /> : undefined}>
      <div className="grid items-center gap-6 md:grid-cols-[auto_1fr]">
        <ProgressRing value={a.total} size={136} label="/ 100 · 可观察互动" />
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {delta != null && (
              <Badge tone={delta > 0 ? "good" : delta < 0 ? "warn" : "neutral"}>{delta > 0 ? `↑ ${delta}` : delta < 0 ? `↓ ${-delta}` : "持平"} 较上次报告</Badge>
            )}
            <Badge tone="neutral">证据 {overview ? Math.round(overview.n_eff) : r.scope.msg_total} 条有效对话</Badge>
            <Badge tone="neutral">覆盖 {Math.round(a.coverage * 100)}%</Badge>
          </div>
          {a.caveats && a.caveats.length > 0 && (
            <p className="text-xs text-ink-3 leading-relaxed">{a.caveats.join("；")}</p>
          )}
          <p className="text-xs text-ink-3 leading-relaxed">描述的是聊天中可观察的互动行为，不是对方真实心理的概率，也不预测结果。</p>
          <Button size="sm" variant="soft" onClick={() => nav(`/report/${r.id}`)}>
            为什么是 {a.total ?? "—"}？ <ArrowRight size={14} strokeWidth={1.8} />
          </Button>
        </div>
      </div>
    </Card>
  );
}

function DimensionStrip({ r, onPick }: { r: Report; onPick: (id: string) => void }) {
  const sparks: Record<string, number[]> = {};
  for (const k of DIM_ORDER) sparks[k] = r.trend.points.map((p) => p.dims?.[k]).filter((v): v is number => v != null);
  const latestPts = r.trend.points.filter((p) => !p.insufficient);
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      {DIM_ORDER.map((k) => {
        const d = r.affinity.dims[k];
        const last = latestPts.length ? latestPts[latestPts.length - 1].dims?.[k] : null;
        const prev = latestPts.length >= 2 ? latestPts[latestPts.length - 2].dims?.[k] : null;
        const delta = last != null && prev != null ? last - prev : null;
        return (
          <MetricCard
            key={k}
            label={DIM_LABELS[k]}
            value={d?.score ?? "—"}
            delta={delta != null && delta !== 0 ? Math.abs(delta) : undefined}
            deltaLabel={delta != null && delta !== 0 ? (delta > 0 ? "较上段" : `较上段 ↓`) : undefined}
            spark={sparks[k]}
            color={`var(--color-dim, var(--color-brand))`}
            hint={d?.note || undefined}
            onClick={() => d?.evidence_ids[0] && onPick(d.evidence_ids[0])}
          />
        );
      })}
    </div>
  );
}

function Insights({ r, onPick }: { r: Report; onPick: (id: string) => void }) {
  const obs = r.observations.slice(0, 3);
  if (!obs.length) return null;
  return (
    <Card title="关键洞察" right={<Badge tone="info">{r.observations.length} 条</Badge>}>
      <ul className="space-y-2.5">
        {obs.map((o) => (
          <li key={o.id} className="flex items-start gap-2.5 text-sm leading-relaxed">
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" aria-hidden />
            <span className="text-ink-2">
              {o.claim}
              <EvidenceChips ids={o.evidence_ids} report={r} onPick={onPick} />
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function RecentEvidence({ r }: { r: Report }) {
  const readings = r.intent_readings.filter((x) => !x.post_hoc).slice(-2);
  if (!readings.length) return null;
  return (
    <div className="space-y-3">
      <SectionTitle hint="语义判断给出的是可能解释，并标明反证。">最近的语义证据</SectionTitle>
      {readings.map((x) => (
        <EvidenceCard
          key={x.msg_id}
          quote={`「${x.surface_text}」`}
          quoteMeta={x.speaker === "other" ? "对方" : "我"}
          interpretation={x.pragmatic_tags.join("、") || "无明显倾向"}
          signals={x.emotions.map((e) => ({ label: e }))}
          alternative={x.candidates[0]?.reading ? `也可能：${x.candidates[0].reading}` : undefined}
          strength={CONF_LABELS[x.evidence_strength]}
        />
      ))}
    </div>
  );
}

function TimelineSection({ overview }: { overview: RelationshipOverview | null }) {
  if (!overview) return null;
  const nodes: TimelineNode[] = [];
  const stages = overview.stage_history.points.filter((p) => p.change);
  for (const p of stages.slice(-5)) {
    nodes.push({
      date: p.created_at.slice(0, 10),
      title: p.change ? `阶段：${STAGE_LABELS[p.change.before] ?? p.change.before} → ${STAGE_LABELS[p.change.after] ?? p.change.after}` : "",
      kind: "milestone",
    });
  }
  if (!nodes.length) return null;
  return (
    <Card title="关系时间线" hint="阶段的变化会被如实记录，而不是被隐藏。">
      <RelationshipTimeline nodes={nodes} />
    </Card>
  );
}

function PriorsPanel({ r }: { r: Report }) {
  const axes = r.persona.inferred_axes;
  const observed: Record<string, number | null> = {};
  for (const k of ["EI", "SN", "TF", "JP"]) {
    const ax = axes[k];
    if (!ax || ax.lean === "unclear") observed[k] = null;
    else {
      const strong = ({ high: 16, medium: 30, low: 42 } as Record<string, number>)[ax.confidence as string] ?? 30;
      const left = k === "EI" ? "E" : k === "SN" ? "S" : k === "TF" ? "T" : "J";
      observed[k] = ax.lean === left ? 50 - strong : 50 + strong;
    }
  }
  const checks = r.prior_checks;
  const self = r.priors.find((p) => p.label.toUpperCase() === "MBTI")?.value ?? null;
  const zodiac = r.priors.find((p) => p.label === "星座")?.value ?? null;
  const czz = r.priors.find((p) => p.label === "属相")?.value ?? null;
  const counts = { supported: 0, contradicted: 0, untested: 0 };
  for (const c of checks) counts[c.status] += 1;
  return (
    <Card
      large
      title="人格先验"
      hint="MBTI / 星座 / 属相只是初识阶段的起始假设；权重随聊天证据衰减，真正说话的是聊天。"
      right={<Badge tone="lavender">先验权重 {Math.round((r.prior_breakdown?.alpha ?? 0) * 100)}%</Badge>}
    >
      <div className="grid gap-5 md:grid-cols-3">
        <div>
          <SectionTitle hint={self ? `自填 ${self}（空心标记）` : "未填 MBTI"}>人格罗盘（聊天观察）</SectionTitle>
          {r.persona.available ? <PersonalityCompass observed={observed} self={self} /> : <p className="text-xs text-ink-3">材料还不足以推断沟通倾向。</p>}
        </div>
        <div className="space-y-3">
          {zodiac ? (
            <ZodiacConstellation zh={ZODIAC_MAP[zodiac] ?? zodiac} en={zodiac} element={ZODIAC_ELEMENT[zodiac] ?? "—"} hint={r.fun.zodiac_pair?.text} />
          ) : (
            <p className="text-xs text-ink-3">星座未填。</p>
          )}
          {checks.length > 0 && (
            <div className="text-xs text-ink-2 leading-relaxed">
              先验卡对照：<b className="text-mint-deep">支持 {counts.supported}</b> · <b className="text-warn">推翻 {counts.contradicted}</b> · 未检验 {counts.untested}
            </div>
          )}
        </div>
        <div className="flex flex-col items-center gap-2">
          <ChineseZodiacOrbit current={czz} />
          <p className="text-xs text-ink-3">{czz ? `属相 ${CZ_MAP[czz] ?? czz}` : "属相未填"}</p>
        </div>
      </div>
      {checks.length > 0 && (
        <div className="mt-4 space-y-1.5 border-t border-line pt-3">
          {checks.slice(0, 4).map((c) => (
            <div key={c.label} className="flex flex-wrap items-center gap-2 text-xs">
              <InsightTag kind="hypothesis">{c.label}</InsightTag>
              <span className="text-ink-2">{c.text}</span>
              <Badge tone={c.status === "supported" ? "good" : c.status === "contradicted" ? "warn" : "neutral"}>{PRIOR_CHECK_LABELS[c.status]}</Badge>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ActionsPanel({ r }: { r: Report }) {
  const sit = r.playbook.situations[0];
  const opts = sit?.options?.slice(0, 2) ?? [];
  if (!opts.length) return null;
  return (
    <Card title="下一步建议" hint="每条都带停止条件；看到它就该停下来。">
      <div className="space-y-3">
        <p className="text-xs text-ink-3">情境：{sit.title}</p>
        {opts.map((o) => (
          <div key={o.id} className="rounded-[var(--radius-sm-card)] border border-line bg-paper p-3.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">{o.goal}</span>
              {o.caution && <Badge tone="warn">{o.caution}</Badge>}
            </div>
            <p className="mt-1 text-sm text-ink-2">{o.action}</p>
            {o.suggested_text && <p className="mt-1.5 border-l-2 pl-3 text-sm text-ink-3" style={{ borderColor: "var(--color-brand-soft)" }}>示例：{o.suggested_text}</p>}
            <p className="mt-1.5 text-xs text-ink-3">停止条件：{o.stop_when}</p>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------- 页面 */

export function WelcomePage() {
  const navigate = useNavigate();
  const paired = !!getCsrf();
  const health = useHealth();
  const [flow] = useFlow();
  const rid = flow.relationshipId;
  const [reports, setReports] = useState<ReportListItem[] | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [overview, setOverview] = useState<RelationshipOverview | null>(null);
  const [pick, setPick] = useState<string | null>(null);

  useEffect(() => {
    if (!paired) return;
    let alive = true;
    setReports(null);
    setReport(null);
    api
      .get<ReportListItem[]>(`/reports?relationship_id=${encodeURIComponent(rid)}`)
      .then((rows) => alive && setReports(rows))
      .catch(() => alive && setReports([]));
    api
      .get<RelationshipOverview>(`/relationships/${rid}/overview`)
      .then((v) => alive && setOverview(v))
      .catch(() => alive && setOverview(null));
    return () => {
      alive = false;
    };
  }, [paired, rid]);

  const latestId = reports?.[0]?.id ?? null;
  useEffect(() => {
    if (!latestId) return;
    let alive = true;
    api
      .get<Report>(`/reports/${latestId}`)
      .then((v) => alive && setReport(v))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [latestId]);

  const relName = overview?.relationship?.name ?? null;
  const hasReport = !!report;


  return (
    <div className="space-y-5">
      <Hero name={relName} hasData={hasReport} />

      {paired && reports && !latestId && (
        <Card large>
          <EmptyState title="故事还没有开始。" hint="导入一段聊天，让关系变得可以被看见。" action={<Button variant="primary" onClick={() => navigate("/import")}>导入聊天记录</Button>} />
        </Card>
      )}

      {paired && hasReport && report && (
        <>
          <Pulse r={report} overview={overview} />
          <div>
            <SectionTitle hint="点开可以看到影响这一维的聊天证据。">五维关系指标</SectionTitle>
            <DimensionStrip r={report} onPick={setPick} />
          </div>
          <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
            <Card title="关系趋势" hint="按周分段；样本不足的分段不计分。">
              <Suspense fallback={<Spinner label="加载趋势…" />}><TrendChart points={report.trend.points} momentum={report.trend.momentum} /></Suspense>
            </Card>
            <TimelineSection overview={overview} />
          </div>
          <div className="grid gap-5 lg:grid-cols-2">
            <Insights r={report} onPick={setPick} />
            <ActionsPanel r={report} />
          </div>
          <RecentEvidence r={report} />
          <PriorsPanel r={report} />
          <PrivacyBadge online={false} model={health ? `v${health.version}` : undefined} />
        </>
      )}

      {paired && reports === null && <Spinner label="读取历史…" />}

      {report && <EvidencePanel id={pick} report={report} onClose={() => setPick(null)} />}
    </div>
  );
}
