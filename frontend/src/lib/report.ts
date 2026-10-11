/** Pure helpers used by the report UI; unit-tested with vitest. */

import type { TrendPoint } from "@/api/types";

export function trendSeries(points: TrendPoint[]): (number | null)[] {
  return points.map((p) => (p.insufficient ? null : p.score));
}

export function formatScore(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : String(v);
}

export function shortId(id: string): string {
  return id.replace(/^m0*/, "#");
}
