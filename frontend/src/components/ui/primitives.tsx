/** 设计系统 · 基础原语（Button / Card / Badge / Tabs / Modal / Tooltip …）。
 *  所有页面与复合组件只从这里取零件，不允许页面内自定义同类控件（docs/DESIGN.md §26）。 */

import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";

/* ------------------------------------------------------------------ Button */

export type ButtonVariant = "primary" | "ghost" | "soft" | "danger";

export function Button({
  variant = "ghost",
  size = "md",
  className = "",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: "sm" | "md" }) {
  return <button className={`btn btn-${variant} ${size === "sm" ? "px-2.5 py-1 text-xs" : ""} ${className}`} {...rest} />;
}

export function IconButton({
  label,
  children,
  className = "",
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button type="button" aria-label={label} title={label} className={`btn btn-ghost p-2 ${className}`} {...rest}>
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ Card */

export function Card({
  title,
  hint,
  children,
  right,
  className = "",
  padding = true,
  hoverable = false,
  large = false,
  onClick,
}: {
  title?: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  right?: ReactNode;
  className?: string;
  padding?: boolean;
  hoverable?: boolean;
  large?: boolean;
  onClick?: () => void;
}) {
  const Tag = (onClick ? "button" : "section") as "section";
  return (
    <Tag
      className={`card ${large ? "card-lg" : ""} ${hoverable || onClick ? "card-hoverable" : ""} ${padding ? "" : "p-0 md:p-0"} ${className}`}
      {...(onClick ? { onClick, type: "button" as never } : {})}
      style={onClick ? ({ textAlign: "left" } as CSSProperties) : undefined}
    >
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 mb-3">
          <div>
            {title && <h2 className="text-base font-semibold">{title}</h2>}
            {hint && <p className="text-xs text-ink-3 mt-0.5">{hint}</p>}
          </div>
          {right}
        </header>
      )}
      {children}
    </Tag>
  );
}

export function SectionTitle({ children, hint, className = "" }: { children: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <div className={`mb-3 ${className}`}>
      <h2 className="text-base font-semibold">{children}</h2>
      {hint && <p className="text-xs text-ink-3 mt-0.5">{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ Badge / Tag */

export type BadgeTone = "neutral" | "good" | "warn" | "info" | "accent" | "brand" | "lavender" | "peach" | "mint";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-paper-2 text-ink-2 border-line",
  good: "bg-good-soft text-good",
  warn: "bg-warn-soft text-warn border-warn-line",
  info: "bg-info-soft text-info",
  accent: "bg-accent-soft text-accent",
  brand: "bg-brand-soft text-brand-ink",
  lavender: "bg-lavender-soft text-lavender-deep",
  peach: "bg-peach-soft text-gold",
  mint: "bg-mint-soft text-mint-deep",
};

export function Badge({ tone = "neutral", children, className = "" }: { tone?: BadgeTone; children: ReactNode; className?: string }) {
  return <span className={`badge ${BADGE_TONES[tone]} ${className}`}>{children}</span>;
}

/** 洞察标签：结论的「语气」（观察 / 假设 / 建议 / 边界）。 */
export function InsightTag({ kind, children }: { kind: "observation" | "hypothesis" | "suggestion" | "boundary"; children: ReactNode }) {
  const cls = { observation: "info", hypothesis: "lavender", suggestion: "brand", boundary: "warn" }[kind] as BadgeTone;
  const label = { observation: "观察", hypothesis: "假设", suggestion: "建议", boundary: "边界" }[kind];
  return (
    <Badge tone={cls}>
      {label}
      {children ? <span className="opacity-80">· {children}</span> : null}
    </Badge>
  );
}

/* ------------------------------------------------------------------ Alert / 空状态 / 错误 */

export function Alert({ tone = "info", title, children, icon }: { tone?: "info" | "warn" | "good" | "brand"; title?: ReactNode; children?: ReactNode; icon?: ReactNode }) {
  const cls = {
    info: "bg-info-soft text-info border-info-soft",
    warn: "bg-warn-soft text-warn border-warn-line",
    good: "bg-good-soft text-good border-good-soft",
    brand: "bg-brand-soft text-brand-ink border-brand-soft",
  }[tone];
  const ic = icon ?? { info: "ℹ", warn: "⚠", good: "✓", brand: "♥" }[tone];
  return (
    <div role={tone === "warn" ? "alert" : "status"} className={`rounded-[14px] border px-3.5 py-2.5 text-sm ${cls}`}>
      <span aria-hidden className="mr-2">{ic}</span>
      {title && <strong className="mr-1">{title}</strong>}
      <span>{children}</span>
    </div>
  );
}

/** 边界与安全的温和提醒：明确拒绝 / 停止联系 / 安全顾虑（spec §18）。 */
export function WarningCard({ title = "这里可能需要多一点注意", children, className = "" }: { title?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-[var(--radius-lg-card)] border bg-warn-soft px-4 py-3.5 ${className}`} style={{ borderColor: "var(--color-warn-line)" }}>
      <div className="flex items-center gap-2 text-sm font-medium text-warn">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden>
          <path d="M12 8.5v5" />
          <circle cx="12" cy="16.8" r="0.4" fill="currentColor" />
          <path d="M10.3 3.6 2.6 17.4a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0Z" />
        </svg>
        {title}
      </div>
      <div className="mt-1.5 text-sm text-ink-2">{children}</div>
    </div>
  );
}

export function Spinner({ label = "加载中" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-3" role="status" aria-live="polite">
      <span className="inline-block h-4 w-4 rounded-full border-2 border-line border-t-brand animate-spin" aria-hidden />
      {label}
    </div>
  );
}

/** 空状态：两个聊天气泡 + 星点（spec §25）。 */
export function EmptyState({
  title = "故事还没有开始。",
  hint,
  action,
  className = "",
}: {
  title?: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`py-10 text-center ${className}`}>
      <svg width="120" height="72" viewBox="0 0 120 72" fill="none" aria-hidden className="mx-auto mb-4">
        <circle cx="14" cy="14" r="1.6" fill="var(--color-lavender)" opacity="0.7" />
        <circle cx="108" cy="10" r="1.4" fill="var(--color-brand)" opacity="0.6" />
        <circle cx="100" cy="60" r="1.8" fill="var(--color-peach)" opacity="0.7" />
        <circle cx="22" cy="62" r="1.2" fill="var(--color-lavender)" opacity="0.5" />
        <rect x="16" y="18" width="46" height="26" rx="10" fill="#fff" stroke="var(--color-brand)" strokeWidth="1.7" opacity="0.85" />
        <path d="M28 44l4-7h-4" stroke="var(--color-brand)" strokeWidth="1.7" strokeLinecap="round" />
        <rect x="58" y="30" width="46" height="26" rx="10" fill="var(--color-brand-mist)" stroke="var(--color-line-2)" strokeWidth="1.7" />
        <path d="M92 56l-4-7h4" stroke="var(--color-line-2)" strokeWidth="1.7" strokeLinecap="round" />
      </svg>
      <p className="text-ink-2 font-medium">{title}</p>
      {hint && <p className="text-sm text-ink-3 mt-1">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** 旧名字保留：Empty = EmptyState 的轻量兼容包装。 */
export function Empty({ children }: { children: ReactNode }) {
  return <EmptyState title={children} />;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as { message?: string; hint?: string; code?: string };
  return (
    <Alert tone="warn" title={e.code ? `[${e.code}]` : undefined}>
      {e.message} {e.hint && <span className="opacity-80">— {e.hint}</span>}
    </Alert>
  );
}

/* ------------------------------------------------------------------ Field / Divider / Tabs */

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="block text-xs text-ink-3 mt-1">{hint}</span>}
    </label>
  );
}

export function Divider({ label }: { label?: ReactNode }) {
  return (
    <div className="flex items-center gap-3 my-4" role="separator">
      <span className="flex-1 border-t border-line" aria-hidden />
      {label && <span className="text-xs text-ink-3">{label}</span>}
      <span className="flex-1 border-t border-line" aria-hidden />
    </div>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
  className = "",
}: {
  tabs: { id: string; label: ReactNode; count?: number }[];
  active: string;
  onChange: (id: string) => void;
  className?: string;
}) {
  return (
    <div className={`inline-flex items-center gap-1 rounded-[var(--radius-input)] border border-line bg-paper-2 p-1 ${className}`} role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={t.id === active}
          onClick={() => onChange(t.id)}
          className={`px-3 py-1 text-sm rounded-[9px] transition-colors ${
            t.id === active ? "bg-card text-brand-ink font-medium shadow-sm" : "text-ink-2 hover:text-ink"
          }`}
        >
          {t.label}
          {t.count != null && <span className="ml-1 text-xs opacity-70">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ Modal / Tooltip */

export function Modal({
  open,
  onClose,
  title,
  children,
  width = 560,
  placement = "center",
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  width?: number;
  placement?: "center" | "left";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const element = ref.current;
    if (!open || !element) return;
    if (!element.open) element.showModal();
    return () => { if (element.open) element.close(); };
  }, [open]);
  return (
    <dialog ref={ref} className={`atelier-dialog atelier-dialog--${placement}`} style={{ maxWidth: width }} aria-labelledby={title ? titleId : undefined} aria-label={title ? undefined : "对话框"}
      onCancel={event => { event.preventDefault(); onClose(); }}
      onClick={event => { if (event.target !== event.currentTarget) return; const box = event.currentTarget.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) onClose(); }}>
      {open && <>
          <header className="atelier-dialog-header flex items-start justify-between gap-3 px-5 pt-5 pb-3 sticky top-0 bg-card/95 backdrop-blur rounded-t-[var(--radius-modal)]">
            <h2 id={titleId} className="text-base font-semibold">{title}</h2>
            <button type="button" onClick={onClose} aria-label="关闭" className="btn btn-ghost p-1.5 -mr-1.5">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden>
                <path d="m6 6 12 12M18 6 6 18" />
              </svg>
            </button>
          </header>
        <div className="px-5 pb-5">{children}</div>
      </>}
    </dialog>
  );
}

export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span
      className="relative inline-flex"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <span aria-describedby={open ? id : undefined} className="inline-flex">{children}</span>
      {open && (
        <span
          role="tooltip"
          id={id}
          className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 whitespace-pre-line w-max max-w-64 rounded-[10px] bg-ink text-white/95 text-xs px-2.5 py-1.5 z-50 anim-fade-in"
          style={{ boxShadow: "var(--shadow-pop)" }}
        >
          {text}
        </span>
      )}
    </span>
  );
}
