/** 关系档案 picker: choose which person the current material / diary / draft belongs to.
 *
 * The selection lives in the flow store so every page reads the same value. Creating a new
 * relationship is inline because the alternative (a separate admin page first) is exactly the
 * kind of friction that makes people dump two people's chats into one scope.
 */

import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { Relationship } from "@/api/types";
import { useFlow } from "@/store";

let cache: Relationship[] | null = null;
const listeners = new Set<() => void>();

export async function refreshRelationships(): Promise<Relationship[]> {
  const r = await api.get<{ relationships: Relationship[] }>("/relationships");
  cache = r.relationships;
  listeners.forEach((l) => l());
  return cache;
}

export function useRelationships(): [Relationship[] | null, () => Promise<Relationship[]>] {
  const [, tick] = useState(0);
  useEffect(() => {
    const l = () => tick((x) => x + 1);
    listeners.add(l);
    if (cache === null) refreshRelationships().catch(() => {});
    return () => {
      listeners.delete(l);
    };
  }, []);
  return [cache, refreshRelationships];
}

export function RelationshipPicker({ label = "这段聊天的对象", hint, compact = false }: { label?: string; hint?: string; compact?: boolean }) {
  const [flow, setFlow] = useFlow();
  const [rels, refresh] = useRelationships();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // a deleted / archived relationship must not linger as the selection
    if (rels && !rels.some((r) => r.id === flow.relationshipId)) setFlow({ relationshipId: "default" });
  }, [rels, flow.relationshipId, setFlow]);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<Relationship>("/relationships", { name: name.trim() });
      await refresh();
      setFlow({ relationshipId: r.id });
      setName("");
      setCreating(false);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = rels?.find((r) => r.id === flow.relationshipId);
  function selectRelationship(id: string) {
    const person = rels?.find((r) => r.id === id)?.person ?? { alias: "" };
    setFlow({ relationshipId: id, profile: { me: flow.profile.me, other: person, goal: "understand", goal_text: "" } });
  }
  return (
    <div className={compact ? "" : "block"}>
      {!compact && <span className="label">{label}</span>}
      <div className="flex flex-wrap items-center gap-2">
        <select className="input w-auto py-1" value={flow.relationshipId} onChange={(e) => selectRelationship(e.target.value)} aria-label={label} disabled={!rels}>
          {(rels ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.name || r.id}{r.is_default ? "（默认）" : ""}{r.counts?.reports ? ` · ${r.counts.reports} 份报告` : ""}
            </option>
          ))}
        </select>
        {creating ? (
          <>
            <input className="input w-40 py-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="对象名称 / 代号" maxLength={60} onKeyDown={(e) => e.key === "Enter" && create()} />
            <button className="btn btn-primary py-1" disabled={busy || !name.trim()} onClick={create}>创建</button>
            <button className="btn btn-ghost py-1" onClick={() => setCreating(false)}>取消</button>
          </>
        ) : (
          <button className="btn btn-ghost py-1 text-xs" onClick={() => setCreating(true)}>＋ 新对象</button>
        )}
      </div>
      {err && <span className="block text-xs text-warn mt-1">{err}</span>}
      {!compact && (
        <span className="block text-xs text-ink-3 mt-1">
          {hint ?? "不同对象的证据、历史曲线、画像与反馈互相隔离；同一个人的多次导入请选同一个对象。"}
          {current?.labels?.mbti && ` 当前对象标签：${current.labels.mbti}。`}
        </span>
      )}
    </div>
  );
}
