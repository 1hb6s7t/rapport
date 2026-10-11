import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { DriveConfig, InboxView, NudgeItem } from "@/api/types";
import { Alert, Badge, Card, ErrorBox, Spinner } from "@/components/ui";
import { PasteToChat } from "@/components/PasteToChat";
import { DRIVE_SOURCE_LABELS, DRIVE_VAR_LABELS, NUDGE_KIND_LABELS } from "@/i18n/labels";

/** 收件箱：主动提醒只在这里出现。每条都能展开看「为什么是现在」。 */
export function InboxPage() {
  const [view, setView] = useState<InboxView | null>(null);
  const [cfg, setCfg] = useState<DriveConfig | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function load() {
    setView(await api.get<InboxView>("/drive/inbox?limit=100"));
    setCfg(await api.get<DriveConfig>("/drive/config").catch(() => null));
  }
  useEffect(() => { load().catch(setErr); }, []);

  const update = (n: NudgeItem) => setView((v) => (v ? { ...v, items: v.items.map((i) => (i.id === n.id ? n : i)), unread: v.items.filter((i) => (i.id === n.id ? n : i).status === "unread").length } : v));
  const setStatus = async (n: NudgeItem, status: NudgeItem["status"]) => update(await api.post<NudgeItem>(`/nudges/${n.id}/${status}`));
  const setVerdict = async (n: NudgeItem, verdict: NudgeItem["verdict"]) => update(await api.put<NudgeItem>(`/nudges/${n.id}/verdict`, { verdict: n.verdict === verdict ? null : verdict }));

  if (err) return <ErrorBox error={err} />;
  if (!view) return <Spinner />;

  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <h1 className="text-xl font-semibold">提醒收件箱</h1>
        <span className="text-sm text-ink-3">{view.unread} 条未读</span>
      </div>
      {cfg && !cfg.enabled && (
        <Alert tone="info">主动提醒当前是关闭的。要开启请到 <Link to="/settings">设置</Link>；开启后每天最多 {cfg.daily_cap} 条，只发给你自己。</Alert>
      )}
      <p className="text-sm text-ink-3">{view.note}</p>
      {view.items.length === 0 && <Card><p className="text-sm text-ink-3">还没有提醒。它不会为了刷存在感而说话：没有可引用的证据时就保持安静。</p></Card>}
      {view.items.map((n) => (
        <Card key={n.id} className={n.status === "unread" ? "border-accent" : n.status === "dismissed" ? "opacity-60" : ""}>
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
            <Badge tone={n.kind === "self_care" ? "info" : n.kind === "checkin" ? "accent" : "neutral"}>{NUDGE_KIND_LABELS[n.kind] ?? n.kind}</Badge>
            <span>{new Date(n.created_at).toLocaleString()}</span>
            {n.status !== "unread" && <span>· {{ read: "已读", dismissed: "已忽略", acted: "已照做" }[n.status]}</span>}
          </div>
          <p className="mt-2 text-sm">{n.text}</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {n.status === "unread" && <button className="btn btn-ghost py-1" onClick={() => setStatus(n, "read")}>标为已读</button>}
            {n.status !== "acted" && n.kind !== "self_care" && <button className="btn btn-ghost py-1" onClick={() => setStatus(n, "acted")}>我照做了</button>}
            {n.status !== "dismissed" && <button className="btn btn-ghost py-1" onClick={() => setStatus(n, "dismissed")}>忽略（之后少来）</button>}
            {n.kind === "draft" && <PasteToChat text={n.text.replace(/^有一条你批准过的话术可以直接用：「|」（严格模式.*$/g, "")} source="nudge" />}
            <span className="ml-auto flex items-center gap-1">
              <button aria-label="有用" className={`px-2 py-1 rounded ${n.verdict === "agree" ? "bg-good-soft" : "hover:bg-paper"}`} onClick={() => setVerdict(n, "agree")}>👍</button>
              <button aria-label="没用" className={`px-2 py-1 rounded ${n.verdict === "disagree" ? "bg-warn-soft" : "hover:bg-paper"}`} onClick={() => setVerdict(n, "disagree")}>👎</button>
            </span>
          </div>
          <button className="mt-2 text-xs text-ink-3 underline" onClick={() => setOpen(open === n.id ? null : n.id)}>
            {open === n.id ? "收起" : "为什么是现在？"}
          </button>
          {open === n.id && (
            <div className="mt-2 text-xs text-ink-2 space-y-1">
              <div>依据：{n.why || "—"}{n.evidence_ids.length > 0 && <span className="mono"> · 证据 {n.evidence_ids.join("、")}</span>}</div>
              <div className="flex flex-wrap gap-3">
                {(["drive", "inhibition", "novelty"] as const).map((k) => (
                  <span key={k}>{DRIVE_VAR_LABELS[k]} <span className="mono">{(n.drive_snapshot[k] ?? 0).toFixed(2)}</span></span>
                ))}
                <span>触发概率 <span className="mono">{n.drive_snapshot.p ?? "—"}</span></span>
              </div>
              {n.drive_snapshot.sources && (
                <div>
                  {Object.entries(n.drive_snapshot.sources).flatMap(([v, srcs]) =>
                    Object.entries(srcs).map(([s, val]) => <span key={v + s} className="inline-block mr-2">{DRIVE_VAR_LABELS[v] ?? v}←{DRIVE_SOURCE_LABELS[s] ?? s} <span className="mono">+{val}</span></span>),
                  )}
                </div>
              )}
              <div className="text-ink-3">参数版本 <span className="mono">{n.version}</span> · 只发到本机收件箱（{n.actuator}）</div>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
