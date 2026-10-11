/** 设计系统 · 数据呈现原语（ProgressRing / Sparkline / MetricCard / EvidenceCard …）。
 *  原则：每个数字都必须能回答「为什么是这个数」。 */

import { useId, type ReactNode } from "react";
import { Badge, Tooltip, type BadgeTone } from "./primitives";

/* ------------------------------------------------------------------ ProgressRing */

export function ProgressRing({
  value,
  size = 120,
  stroke = 10,
  label,
  sublabel,
  color = "var(--color-brand)",
  track = "var(--color-brand-soft)",
}: {
  value: number | null;
  size?: number;
  stroke?: number;
  label?: ReactNode;
  sublabel?: ReactNode;
  color?: string;
  track?: string;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }} role="img" aria-label={value == null ? "暂无数据" : `${value}`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={track} strokeWidth={stroke} strokeLinecap="round" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - v / 100)}
          style={{ transition: "stroke-dashoffset 600ms var(--ease-soft)" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-semibold tabular-nums">{value == null ? "—" : value}</span>
        {label && <span className="text-xs text-ink-3">{label}</span>}
        {sublabel}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ Sparkline */

export function Sparkline({
  points,
  width = 96,
  height = 28,
  color = "var(--color-brand)",
  area = true,
}: {
  points: number[];
  width?: number;
  height?: number;
  color?: string;
  area?: boolean;
}) {
  const id = useId().replace(/[:]/g, "");
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const xs = points.map((_, i) => (i / (points.length - 1)) * (width - 2) + 1);
  const ys = points.map((p) => height - 3 - ((p - min) / span) * (height - 6));
  const d = xs.map((x, i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden className="block">
      {area && (
        <>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor={color} stopOpacity="0.28" />
              <stop offset="1" stopColor={color} stopOpacity="0.02" />
            </linearGradient>
          </defs>
          <path d={`${d} L${xs[xs.length - 1]} ${height} L${xs[0]} ${height} Z`} fill={`url(#${id})`} />
        </>
      )}
      <path d={d} fill="none" stroke={color} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* ------------------------------------------------------------------ MetricCard / StatTile */

export function MetricCard({
  label,
  value,
  delta,
  deltaLabel,
  spark,
  color,
  onClick,
  hint,
}: {
  label: ReactNode;
  value: ReactNode;
  delta?: ReactNode;
  deltaLabel?: ReactNode;
  spark?: number[];
  color?: string;
  onClick?: () => void;
  hint?: string;
}) {
  const body = (
    <div className="flex items-end justify-between gap-2">
      <div className="min-w-0">
        <p className="text-xs text-ink-3 truncate">{label}</p>
        <div className="mt-1 flex items-baseline gap-1.5">
          <span className="text-xl font-semibold tabular-nums">{value}</span>
          {delta && <span className="text-xs font-medium text-mint-deep">▲ {delta}</span>}
          {deltaLabel && <span className="text-[11px] text-ink-3">{deltaLabel}</span>}
        </div>
      </div>
      {spark && <Sparkline points={spark} color={color} />}
    </div>
  );
  const card = (
    <div className={`card p-3.5 ${onClick ? "card-hoverable" : ""}`} onClick={onClick} role={onClick ? "button" : undefined}>
      {body}
    </div>
  );
  return hint ? <Tooltip text={hint}>{card}</Tooltip> : card;
}

export function StatTile({ label, value, sub }: { label: ReactNode; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-sm-card)] border border-line bg-paper px-3.5 py-3 min-w-0">
      <p className="text-xs text-ink-3 truncate">{label}</p>
      <p className="mt-0.5 text-lg font-semibold tabular-nums truncate">{value}</p>
      {sub && <p className="text-[11px] text-ink-3 truncate">{sub}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ ConfidenceBadge */

export function ConfidenceBadge({ level, pct }: { level?: "low" | "medium" | "high" | string | null; pct?: number }) {
  const lv = level === "high" ? "high" : level === "medium" ? "medium" : "low";
  const tone: BadgeTone = ({ high: "good", medium: "lavender", low: "neutral" } as Record<string, BadgeTone>)[lv];
  const label = pct != null ? `${Math.round(pct)}%` : { high: "较高", medium: "中等", low: "较低" }[lv];
  return (
    <Tooltip text="置信度是模型的自评，不是正确率；已实测它会自信地错。">
      <Badge tone={tone}>置信 {label}</Badge>
    </Tooltip>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { tone: BadgeTone; label: string }> = {
    completed: { tone: "good", label: "已完成" },
    ready: { tone: "good", label: "就绪" },
    running: { tone: "brand", label: "运行中" },
    pending: { tone: "neutral", label: "等待" },
    failed: { tone: "warn", label: "失败" },
    experimental: { tone: "lavender", label: "实验性" },
  };
  const s = map[status] ?? { tone: "neutral" as BadgeTone, label: status };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

/* ------------------------------------------------------------------ EvidenceCard
   AI 判断 → 证据 的核心交互单元（spec §11）。*/

export function EvidenceCard({
  quote,
  quoteMeta,
  interpretation,
  signals,
  alternative,
  confidence,
  strength,
  className = "",
}: {
  quote: ReactNode;
  quoteMeta?: ReactNode;
  interpretation: ReactNode;
  signals?: { label: string }[];
  alternative?: ReactNode;
  confidence?: number;
  strength?: string;
  className?: string;
}) {
  return (
    <figure className={`rounded-[var(--radius-sm-card)] border border-line bg-paper px-4 py-3.5 ${className}`}>
      <blockquote className="border-l-2 pl-3 text-sm leading-relaxed text-ink" style={{ borderColor: "var(--color-brand)" }}>
        {quote}
      </blockquote>
      {quoteMeta && <p className="mt-1 text-xs text-ink-3 pl-3">{quoteMeta}</p>}
      <div className="mt-3 grid gap-1.5 text-sm">
        <div className="flex gap-2">
          <span className="shrink-0 text-xs text-brand-ink w-14 pt-0.5">解读</span>
          <span className="text-ink-2">{interpretation}</span>
        </div>
        {signals && signals.length > 0 && (
          <div className="flex gap-2 items-start">
            <span className="shrink-0 text-xs text-brand-ink w-14 pt-0.5">证据</span>
            <span className="flex flex-wrap gap-1">
              {signals.map((s) => (
                <span key={s.label} className="chip">{s.label}</span>
              ))}
            </span>
          </div>
        )}
        {alternative && (
          <div className="flex gap-2">
            <span className="shrink-0 text-xs text-brand-ink w-14 pt-0.5">也可能是</span>
            <span className="text-ink-3">{alternative}</span>
          </div>
        )}
      </div>
      <figcaption className="mt-2.5 flex items-center gap-2">
        {confidence != null && <ConfidenceBadge pct={confidence} level={confidence >= 75 ? "high" : confidence >= 45 ? "medium" : "low"} />}
        {strength && <span className="text-[11px] text-ink-3">证据强度 {strength}</span>}
        <span className="text-[11px] text-ink-3">这是可能解释，不是结论。</span>
      </figcaption>
    </figure>
  );
}
