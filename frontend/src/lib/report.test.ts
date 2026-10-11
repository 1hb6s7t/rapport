import { describe, expect, it } from "vitest";
import { formatScore, shortId, trendSeries } from "./report";

describe("report helpers", () => {
  it("maps insufficient points to null, never zero", () => {
    const pts = [
      { label: "a", start: null, end: null, score: 60, n_msgs: 10, n_other_text: 5, insufficient: false, dims: {} },
      { label: "b", start: null, end: null, score: null, n_msgs: 3, n_other_text: 1, insufficient: true, dims: {} },
    ];
    expect(trendSeries(pts)).toEqual([60, null]);
  });
  it("formats missing scores as a dash", () => {
    expect(formatScore(null)).toBe("—");
    expect(formatScore(58)).toBe("58");
  });
  it("shortens message ids", () => {
    expect(shortId("m000012")).toBe("#12");
  });
});
