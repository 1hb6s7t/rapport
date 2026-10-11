/** Tiny cross-page store persisted to sessionStorage (ids only, never chat content). */

import { useCallback, useEffect, useState } from "react";
import type { Profile } from "@/api/types";

interface Flow {
  relationshipId: string;
  importId: string | null;
  analysisId: string | null;
  reportId: string | null;
  mode: "rules" | "byok";
  profile: Profile;
}

const KEY = "rapport_flow";
const DEFAULT: Flow = {
  relationshipId: "default",
  importId: null,
  analysisId: null,
  reportId: null,
  mode: "rules",
  profile: { me: { alias: "" }, other: { alias: "" }, goal: "understand", goal_text: "" },
};

function load(): Flow {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return DEFAULT;
    const { profile: _privateProfile, ...ids } = JSON.parse(raw);
    return { ...DEFAULT, ...ids, profile: DEFAULT.profile };
  } catch {
    return DEFAULT;
  }
}

const listeners = new Set<() => void>();
let state: Flow = load();

export function getFlowSnapshot(): Readonly<Flow> {
  return state;
}

export function setFlow(patch: Partial<Flow>) {
  const changedFriend = patch.relationshipId !== undefined && patch.relationshipId !== state.relationshipId;
  state = {
    ...state,
    ...(changedFriend ? {
      importId: null, analysisId: null, reportId: null,
      profile: { ...DEFAULT.profile, me: state.profile.me, other: { alias: "" } },
    } : {}),
    ...patch,
  };
  try {
    const { profile: _privateProfile, ...ids } = state;
    sessionStorage.setItem(KEY, JSON.stringify(ids));
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}

export function resetFlow() {
  setFlow({ ...DEFAULT });
}

export function useFlow(): [Flow, (p: Partial<Flow>) => void] {
  const [, tick] = useState(0);
  useEffect(() => {
    const l = () => tick((x) => x + 1);
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);
  const set = useCallback((p: Partial<Flow>) => setFlow(p), []);
  return [state, set];
}
