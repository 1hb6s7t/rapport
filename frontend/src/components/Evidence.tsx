import type { Report } from "@/api/types";

export function EvidenceChips({ ids, report, onPick }: { ids: string[]; report: Report; onPick: (id: string) => void }) {
  if (!ids.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1 align-middle ml-1">
      {ids.map((id) => {
        const e = report.evidence[id];
        return (
          <button key={id} type="button" className="badge bg-paper border border-line hover:bg-accent-soft mono" title={e ? `${e.sender === "me" ? "我" : "对方"}: ${e.text}` : id} onClick={() => onPick(id)}>
            {id.replace(/^m0*/, "#")}
          </button>
        );
      })}
    </span>
  );
}

export function EvidencePanel({ id, report, onClose }: { id: string | null; report: Report; onClose: () => void }) {
  if (!id) return null;
  const e = report.evidence[id];
  return (
    <div className="fixed inset-x-0 bottom-0 md:inset-auto md:right-4 md:bottom-4 md:w-96 card shadow-lg z-20" role="dialog" aria-label="证据片段">
      <div className="flex items-start justify-between gap-2">
        <div className="text-xs text-ink-3 mono">{id}</div>
        <button className="text-sm text-ink-3" onClick={onClose} aria-label="关闭">✕</button>
      </div>
      {e ? (
        <div className="mt-2">
          <div className="text-xs text-ink-3">{e.sender === "me" ? "我" : e.sender === "other" ? "对方" : e.sender}{e.ts ? ` · ${new Date(e.ts).toLocaleString()}` : ""}</div>
          <p className="text-sm mt-1 whitespace-pre-wrap">{e.text}</p>
        </div>
      ) : (
        <p className="text-sm text-ink-3 mt-2">该片段不在保留的最小证据中（原始材料可能已按保留策略清理）。</p>
      )}
    </div>
  );
}
