/** 设计系统 · 品牌母题与专属图形（docs/DESIGN.md §4）。
 *  Relationship Orbit / Personality Compass / Zodiac / Chinese Zodiac Orbit / Timeline。
 *  全部是纯 SVG 自绘，无外部图片依赖。 */

import type { ReactNode } from "react";
import { Badge } from "./primitives";

/* ------------------------------------------------------------------ Relationship Orbit（品牌母题） */

const ZODIAC_NODES = 12;

export function RelationshipOrbit({ size = 220, animate = true, className = "" }: { size?: number; animate?: boolean; className?: string }) {
  const S = 200; // viewBox
  const cx = S / 2;
  const cy = S / 2;
  const rings = [88, 66, 46];
  const nodes = Array.from({ length: ZODIAC_NODES }, (_, i) => {
    const a = (i / ZODIAC_NODES) * Math.PI * 2 - Math.PI / 2;
    return { x: cx + Math.cos(a) * rings[0], y: cy + Math.sin(a) * rings[0], i };
  });
  const quads = Array.from({ length: 4 }, (_, i) => {
    const a = (i / 4) * Math.PI * 2;
    return { x: cx + Math.cos(a) * rings[1], y: cy + Math.sin(a) * rings[1], i };
  });
  return (
    <svg width={size} height={size} viewBox={`0 0 ${S} ${S}`} aria-hidden className={className} style={{ overflow: "visible" }}>
      <defs>
        <linearGradient id="orbit-core" x1="60" y1="70" x2="140" y2="135" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#f47c9c" />
          <stop offset="1" stopColor="#e9688a" />
        </linearGradient>
      </defs>
      {/* 轨道环 */}
      {rings.map((r, i) => (
        <circle key={r} cx={cx} cy={cy} r={r} fill="none" stroke={i === 0 ? "#f0dfe6" : "#f6e3ea"} strokeWidth="1" strokeDasharray={i === 1 ? "2 5" : undefined} />
      ))}
      {/* 外环节点：聊天证据 / 生肖节点 */}
      <g className={animate ? "orbit-anim" : undefined} style={{ transformOrigin: `${cx}px ${cy}px`, animation: "orbit-rotate 28s linear infinite" }}>
        {nodes.map((n) => (
          <circle key={n.i} cx={n.x} cy={n.y} r={n.i % 4 === 0 ? 3 : 2} fill={n.i % 3 === 0 ? "var(--color-brand)" : "#e7b6c6"} />
        ))}
      </g>
      {/* 内环：MBTI 四象限 */}
      <g className={animate ? "orbit-anim" : undefined} style={{ transformOrigin: `${cx}px ${cy}px`, animation: "orbit-rotate-rev 40s linear infinite" }}>
        {quads.map((q) => (
          <rect key={q.i} x={q.x - 3.4} y={q.y - 3.4} width={6.8} height={6.8} rx={2} fill="none" stroke="var(--color-lavender)" strokeWidth="1.3" />
        ))}
      </g>
      {/* 星点 */}
      <circle cx={cx - 58} cy={cy - 42} r="1.4" fill="var(--color-lavender)" opacity="0.8" />
      <circle cx={cx + 52} cy={cy + 46} r="1.2" fill="var(--color-peach)" opacity="0.8" />
      {/* 中心：两个叠合的聊天气泡，留白处藏着一颗心 */}
      <g>
        <rect x={cx - 34} y={cy - 24} width={46} height={30} rx={11} fill="url(#orbit-core)" opacity="0.92" />
        <path d={`M${cx - 22} ${cy + 6} l5 8 h-5`} fill="url(#orbit-core)" />
        <rect x={cx - 10} y={cy - 8} width={42} height={28} rx={11} fill="#fff" stroke="#f47c9c" strokeWidth="1.6" />
        <path d={`M${cx + 18} ${cy + 20} l-5 -8 h5`} fill="#fff" stroke="#f47c9c" strokeWidth="1.4" strokeLinejoin="round" />
        <path
          d={`M${cx} ${cy + 7} c-6 -6.5 -13 -4.4 -13 0.8 0 3.9 5.6 7.6 13 12.2 7.4 -4.6 13 -8.3 13 -12.2 0 -5.2 -7 -7.3 -13 -0.8Z`}
          fill="none"
          stroke="var(--color-brand-strong)"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
      </g>
    </svg>
  );
}

/* ------------------------------------------------------------------ Personality Compass（MBTI） */

const AXES: { key: string; left: string; right: string; leftFull: string; rightFull: string }[] = [
  { key: "EI", left: "E", right: "I", leftFull: "外向", rightFull: "内向" },
  { key: "SN", left: "S", right: "N", leftFull: "具体", rightFull: "抽象" },
  { key: "TF", left: "T", right: "F", leftFull: "逻辑", rightFull: "情感" },
  { key: "JP", left: "J", right: "P", leftFull: "计划", rightFull: "随性" },
];

export function PersonalityCompass({
  observed,
  self,
  className = "",
}: {
  /** 观察值：0 = 左极（E/S/T/J），100 = 右极（I/N/F/P）。null = 信息不足。 */
  observed: Record<string, number | null>;
  /** 自填 MBTI 四个字母（如 "INTJ"），画成第二枚标记。 */
  self?: string | null;
  className?: string;
}) {
  return (
    <div className={`space-y-3.5 ${className}`}>
      {AXES.map((ax) => {
        const v = observed[ax.key];
        const pct = v == null ? null : Math.max(0, Math.min(100, v));
        const selfIdx = self ? "EISTFJNP".indexOf(self) : -1;
        const selfLetter = self && self.length === 4 ? self[AXES.findIndex((a) => a.key === ax.key)] : null;
        const selfPct = selfLetter ? (selfLetter === ax.left ? 8 : selfLetter === ax.right ? 92 : null) : null;
        void selfIdx;
        return (
          <div key={ax.key}>
            <div className="flex justify-between text-xs text-ink-3 mb-1">
              <span>
                <b className="text-ink-2">{ax.left}</b> {ax.leftFull}
              </span>
              <span>
                {ax.rightFull} <b className="text-ink-2">{ax.right}</b>
              </span>
            </div>
            <div className="relative h-2.5 rounded-full" style={{ background: "linear-gradient(90deg, var(--color-lavender-soft), var(--color-brand-soft))" }}>
              <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 h-3.5 w-px bg-line-2" aria-hidden />
              {selfPct != null && (
                <span
                  className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 h-3.5 w-3.5 rounded-full border-2 border-lavender bg-card"
                  style={{ left: `${selfPct}%` }}
                  title={`自填 MBTI：${self}`}
                  aria-label={`自填 ${self}`}
                />
              )}
              {pct != null ? (
                <span
                  className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 h-4 w-4 rounded-full shadow-sm"
                  style={{ left: `${pct}%`, background: "linear-gradient(135deg, var(--color-brand), var(--color-brand-2))" }}
                  title={`聊天观察：偏${pct >= 50 ? ax.right : ax.left} ${pct >= 50 ? pct : 100 - pct}%`}
                  aria-label={`观察偏${pct >= 50 ? ax.right : ax.left}`}
                />
              ) : (
                <span className="absolute left-1/2 -translate-x-1/2 top-full mt-0.5 text-[10px] text-ink-3">信息不足</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ Zodiac Constellation（星座） */

const STARFIELD: [number, number, number][] = [
  [18, 14, 1.2], [44, 8, 1.6], [76, 18, 1.1], [96, 10, 1.4], [112, 30, 1.2],
  [30, 38, 1.3], [62, 42, 1.7], [88, 52, 1.1], [14, 62, 1.5], [48, 70, 1.2], [104, 74, 1.4],
];

export function ZodiacConstellation({
  zh,
  en,
  element,
  hint,
  className = "",
}: {
  zh: string;
  en: string;
  element: string;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-[var(--radius-lg-card)] border border-line p-4 ${className}`} style={{ background: "#fff8fb" }}>
      <svg width="126" height="82" viewBox="0 0 126 82" aria-hidden className="block mb-2">
        {STARFIELD.slice(0, 6).map(([x, y], i) =>
          i + 1 < 6 ? <line key={`l${i}`} x1={x} y1={y} x2={STARFIELD[i + 1][0]} y2={STARFIELD[i + 1][1]} stroke="#eab2c1" strokeWidth="0.8" opacity="0.8" /> : null,
        )}
        {STARFIELD.map(([x, y, r], i) => (
          <circle key={i} cx={x} cy={y} r={r} fill={i % 4 === 0 ? "#e9688a" : "#eab2c1"} />
        ))}
      </svg>
      <div className="flex items-baseline gap-2">
        <span className="text-base font-semibold">{zh}</span>
        <span className="text-xs text-ink-3">{en}</span>
        <Badge tone="neutral" className="ml-auto">元素 {element}</Badge>
      </div>
      {hint && <p className="mt-1.5 text-xs text-ink-2 leading-relaxed">{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ Chinese Zodiac Orbit（生肖） */

export const ZODIAC_CN: { key: string; zh: string; emoji?: string }[] = [
  { key: "rat", zh: "鼠" }, { key: "ox", zh: "牛" }, { key: "tiger", zh: "虎" }, { key: "rabbit", zh: "兔" },
  { key: "dragon", zh: "龙" }, { key: "snake", zh: "蛇" }, { key: "horse", zh: "马" }, { key: "goat", zh: "羊" },
  { key: "monkey", zh: "猴" }, { key: "rooster", zh: "鸡" }, { key: "dog", zh: "狗" }, { key: "pig", zh: "猪" },
];

export function ChineseZodiacOrbit({ current, size = 168, className = "" }: { current?: string | null; size?: number; className?: string }) {
  const S = 160;
  const cx = S / 2;
  const cy = S / 2;
  const r = 58;
  return (
    <div className={`inline-block ${className}`}>
      <svg width={size} height={size} viewBox={`0 0 ${S} ${S}`} role="img" aria-label={current ? `属相 ${ZODIAC_CN.find((z) => z.key === current)?.zh ?? current}` : "属相未填"}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--color-brand-soft)" strokeWidth="1.2" />
        <circle cx={cx} cy={cy} r={r - 12} fill="none" stroke="var(--color-lavender-soft)" strokeWidth="1" strokeDasharray="2 5" />
        {ZODIAC_CN.map((z, i) => {
          const a = (i / ZODIAC_CN.length) * Math.PI * 2 - Math.PI / 2;
          const x = cx + Math.cos(a) * r;
          const y = cy + Math.sin(a) * r;
          const active = current === z.key;
          return (
            <g key={z.key}>
              <circle cx={x} cy={y} r={active ? 12 : 8.5} fill={active ? "var(--color-brand)" : "#fff"} stroke={active ? "var(--color-brand-strong)" : "var(--color-line-2)"} strokeWidth="1.2" />
              <text x={x} y={y + 3.4} textAnchor="middle" fontSize={active ? 10 : 8.6} fill={active ? "#fff" : "var(--color-ink-3)"} fontWeight={active ? 600 : 400}>
                {z.zh}
              </text>
            </g>
          );
        })}
        <circle cx={cx} cy={cy} r={4.5} fill="none" stroke="var(--color-brand)" strokeWidth="1.4" />
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ Relationship Timeline */

export type TimelineNode = { date: string; title: ReactNode; kind?: "chat" | "invite" | "warn" | "milestone"; onOpen?: () => void };

export function RelationshipTimeline({ nodes, className = "" }: { nodes: TimelineNode[]; className?: string }) {
  const icon = { chat: "💬", invite: "🤝", warn: "△", milestone: "♥" };
  const tone = { chat: "var(--color-lavender)", invite: "var(--color-mint-deep)", warn: "var(--color-danger)", milestone: "var(--color-brand)" };
  return (
    <ol className={`relative space-y-4 pl-6 ${className}`}>
      <span className="absolute left-[9px] top-1 bottom-1 w-px bg-line-2" aria-hidden />
      {nodes.map((n, i) => (
        <li key={i} className="relative">
          <span
            className="absolute -left-6 top-1 grid place-items-center h-[19px] w-[19px] rounded-full bg-card border"
            style={{ borderColor: "var(--color-line-2)", color: tone[n.kind ?? "milestone"], fontSize: 9 }}
            aria-hidden
          >
            {icon[n.kind ?? "milestone"]}
          </span>
          {n.onOpen ? (
            <button type="button" onClick={n.onOpen} className="text-left group">
              <span className="block text-[11px] text-ink-3">{n.date}</span>
              <span className="text-sm text-ink-2 group-hover:text-brand-ink transition-colors underline-offset-2 group-hover:underline">{n.title}</span>
            </button>
          ) : (
            <div>
              <span className="block text-[11px] text-ink-3">{n.date}</span>
              <span className="text-sm text-ink-2">{n.title}</span>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

/* ------------------------------------------------------------------ PrivacyBadge / ModelCard */

export function PrivacyBadge({ online = false, model }: { online?: boolean; model?: string }) {
  return (
    <div className="rounded-[var(--radius-lg-card)] border p-3.5" style={{ borderColor: "var(--color-good-soft)", background: "var(--color-mint-soft)" }}>
      <div className="flex items-center gap-2 text-sm font-medium text-mint-deep">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M12 2.5 4.5 5.5v6c0 4.6 3.1 8.4 7.5 10 4.4-1.6 7.5-5.4 7.5-10v-6L12 2.5Z" />
          <path d="m8.8 12 2.2 2.2 4.4-4.6" />
        </svg>
        {online ? "云端模式（每次出网前预览确认）" : "本地模式 · 数据不出这台电脑"}
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-ink-2">
        <div className="flex justify-between gap-2"><dt className="text-ink-3">数据位置</dt><dd>本机</dd></div>
        <div className="flex justify-between gap-2"><dt className="text-ink-3">网络</dt><dd>{online ? "按需出网" : "离线可用"}</dd></div>
        <div className="flex justify-between gap-2"><dt className="text-ink-3">模型</dt><dd className="truncate">{model ?? "—"}</dd></div>
        <div className="flex justify-between gap-2"><dt className="text-ink-3">遥测</dt><dd>无</dd></div>
      </dl>
    </div>
  );
}

export function ModelCard({
  name,
  sizeGb,
  status,
  gpu,
  current = false,
  actions,
}: {
  name: string;
  sizeGb?: number;
  status: "ready" | "missing" | "running" | "experimental";
  gpu?: string;
  current?: boolean;
  actions?: ReactNode;
}) {
  const tone = { ready: "good", running: "good", missing: "neutral", experimental: "lavender" }[status] as "good" | "neutral" | "lavender";
  const label = { ready: "就绪", running: "运行中", missing: "未拉取", experimental: "实验性" }[status];
  return (
    <div className={`card p-4 ${current ? "ring-1 ring-brand" : ""}`}>
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">{name}</span>
        <Badge tone={tone}>{label}</Badge>
        {current && <Badge tone="brand">当前</Badge>}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1 text-xs text-ink-3">
        {sizeGb != null && <span>体积 {sizeGb} GB</span>}
        {gpu && <span className="truncate">{gpu}</span>}
      </div>
      {actions && <div className="mt-3 flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

/** 模型拉取的进度环（spec §20：不要终端日志）。 */
export function PullProgressRing({ pct, model }: { pct: number; model?: string }) {
  const S = 44;
  const r = 17;
  const c = 2 * Math.PI * r;
  return (
    <span className="inline-flex items-center gap-2">
      <svg width={S} height={S} viewBox={`0 0 ${S} ${S}`} className="-rotate-90" aria-hidden>
        <circle cx={S / 2} cy={S / 2} r={r} fill="none" stroke="var(--color-brand-soft)" strokeWidth="5" />
        <circle cx={S / 2} cy={S / 2} r={r} fill="none" stroke="var(--color-brand-strong)" strokeWidth="5" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c * (1 - Math.max(0, Math.min(100, pct)) / 100)} style={{ transition: "stroke-dashoffset 400ms var(--ease-soft)" }} />
      </svg>
      <span className="text-xs text-ink-2">
        {model ? <span className="mono">{model} </span> : null}
        {Math.round(pct)}%
      </span>
    </span>
  );
}
