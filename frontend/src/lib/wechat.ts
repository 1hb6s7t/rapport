import { useEffect, useState } from "react";
import { api } from "@/api/client";

export interface WechatBinding { token: string; import_id: string; relationship_id: string }
export interface WechatTarget { contact: string; import_id: string; relationship_id: string; filled: boolean; allow_fill: boolean }
const KEY = "rapport_wechat_target_v1";
export function rememberWechatBinding(binding: WechatBinding) {
  // Capability ids only; screenshots and chat text never enter browser storage.
  sessionStorage.setItem(KEY, JSON.stringify(binding));
}
export function readWechatBinding(importId: string, relationshipId: string): WechatBinding | null {
  try {
    const binding = JSON.parse(sessionStorage.getItem(KEY) || "null") as WechatBinding | null;
    return binding?.import_id === importId && binding.relationship_id === relationshipId && typeof binding.token === "string" ? binding : null;
  } catch { return null; }
}
export function useWechatTarget(importId: string, relationshipId: string) {
  const binding = readWechatBinding(importId, relationshipId);
  const token = binding?.token ?? null;
  const [target, setTarget] = useState<WechatTarget | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setTarget(null); setError(null);
    if (!token) return;
    let alive = true;
    api.post<WechatTarget>("/wechat/target", { token, relationship_id: relationshipId })
      .then(value => { if (alive) setTarget(value); }).catch(value => { if (alive) setError(value); });
    return () => { alive = false; };
  }, [token, relationshipId, importId]);
  return { token, target, error };
}
