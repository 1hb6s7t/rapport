/** 👍 / 👎 / ? on one claim of one report.
 *
 * Feedback is a record of what the user thinks, stored locally and never used to alter the
 * report. Intent verdicts can carry a corrected tag set so they become evaluation cases.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { FeedbackItem, FeedbackKind, Verdict } from "@/api/types";

interface Ctx {
  reportId: string;
  items: Record<string, FeedbackItem>;
  submit: (kind: FeedbackKind, key: string, verdict: Verdict, extra?: { note?: string; correction?: Record<string, unknown> }) => Promise<void>;
  remove: (kind: FeedbackKind, key: string) => Promise<void>;
  disabled: boolean;
}

const FeedbackCtx = createContext<Ctx | null>(null);

function k(kind: string, key: string) {
  return `${kind}:${key}`;
}

export function FeedbackProvider({ reportId, disabled = false, children }: { reportId: string; disabled?: boolean; children: React.ReactNode }) {
  const [items, setItems] = useState<Record<string, FeedbackItem>>({});
  useEffect(() => {
    if (disabled) return;
    api
      .get<{ items: FeedbackItem[] }>(`/reports/${reportId}/feedback`)
      .then((r) => setItems(Object.fromEntries(r.items.map((i) => [k(i.target_kind, i.target_key), i]))))
      .catch(() => {});
  }, [reportId, disabled]);

  const submit = useCallback(
    async (kind: FeedbackKind, key: string, verdict: Verdict, extra?: { note?: string; correction?: Record<string, unknown> }) => {
      const item = await api.post<FeedbackItem>(`/reports/${reportId}/feedback`, { target_kind: kind, target_key: key, verdict, note: extra?.note ?? "", correction: extra?.correction ?? {} });
      setItems((prev) => ({ ...prev, [k(kind, key)]: item }));
    },
    [reportId],
  );
  const remove = useCallback(
    async (kind: FeedbackKind, key: string) => {
      const item = items[k(kind, key)];
      if (!item) return;
      await api.del(`/feedback/${item.id}`);
      setItems((prev) => {
        const next = { ...prev };
        delete next[k(kind, key)];
        return next;
      });
    },
    [items],
  );
  const value = useMemo(() => ({ reportId, items, submit, remove, disabled }), [reportId, items, submit, remove, disabled]);
  return <FeedbackCtx.Provider value={value}>{children}</FeedbackCtx.Provider>;
}

const VERDICTS: [Verdict, string, string][] = [
  ["agree", "👍", "说得对"],
  ["disagree", "👎", "不对"],
  ["unsure", "？", "说不准"],
];

export function FeedbackButtons({ kind, target, options }: { kind: FeedbackKind; target: string; options?: string[] }) {
  const ctx = useContext(FeedbackCtx);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  if (!ctx || ctx.disabled) return null;
  const current = ctx.items[k(kind, target)];

  async function send(verdict: Verdict) {
    setBusy(true);
    try {
      if (current?.verdict === verdict && !open) {
        await ctx!.remove(kind, target);
      } else if (verdict === "disagree" && options && !open) {
        setOpen(true);
        setPicked(((current?.correction as { pragmatic_tags?: string[] } | undefined)?.pragmatic_tags ?? []) as string[]);
      } else {
        const correction = options && picked.length ? { pragmatic_tags: picked } : {};
        await ctx!.submit(kind, target, verdict, { note, correction });
        setOpen(false);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-1 align-middle ml-2" aria-label="对这条判断的反馈">
      {VERDICTS.map(([v, icon, title]) => (
        <button
          key={v}
          type="button"
          title={title}
          aria-pressed={current?.verdict === v}
          disabled={busy}
          className={`badge border text-xs px-1.5 ${current?.verdict === v ? "bg-ink text-white border-ink" : "bg-paper border-line text-ink-3 hover:bg-accent-soft"}`}
          onClick={() => send(v)}
        >
          {icon}
        </button>
      ))}
      {current?.note && !open && <span className="text-xs text-ink-3">「{current.note}」</span>}
      {open && (
        <span className="flex flex-wrap items-center gap-1 w-full mt-1">
          {options && (
            <span className="inline-flex flex-wrap gap-1">
              {options.map((o) => (
                <label key={o} className={`badge border cursor-pointer text-xs ${picked.includes(o) ? "bg-accent-soft border-accent" : "bg-paper border-line"}`}>
                  <input type="checkbox" className="sr-only" checked={picked.includes(o)} onChange={(e) => setPicked(e.target.checked ? [...picked, o] : picked.filter((x) => x !== o))} />
                  {o}
                </label>
              ))}
            </span>
          )}
          <input className="input py-0.5 text-xs w-56" value={note} onChange={(e) => setNote(e.target.value)} placeholder="为什么不对？（可选）" maxLength={300} />
          <button type="button" className="btn btn-primary py-0.5 text-xs" disabled={busy} onClick={() => send("disagree")}>记录</button>
          <button type="button" className="btn btn-ghost py-0.5 text-xs" onClick={() => setOpen(false)}>取消</button>
        </span>
      )}
    </span>
  );
}
