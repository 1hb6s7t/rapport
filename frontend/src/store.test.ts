import { beforeEach, describe, expect, it, vi } from "vitest";

let saved: Map<string, string>;

beforeEach(() => {
  vi.resetModules();
  saved = new Map();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => saved.set(key, value),
  });
});

describe("relationship flow boundaries", () => {
  it("clears old friend's draft profile and evidence on every switch", async () => {
    const { setFlow, getFlowSnapshot } = await import("./store");
    setFlow({ relationshipId: "a", importId: "import-a", reportId: "report-a", analysisId: "analysis-a", profile: {
      me: { alias: "本人", mbti: "INFP" },
      other: { alias: "朋友 A", mbti: "ENTJ", birthday: { year: 1998, calendar: "gregorian", precision: "year" } },
      goal: "repair", goal_text: "朋友 A 的私有补充", stage_confirmed: "mutually_confirmed",
    } });
    setFlow({ relationshipId: "b" });
    const flow = getFlowSnapshot();
    expect(flow.relationshipId).toBe("b");
    expect(flow.importId).toBeNull();
    expect(flow.reportId).toBeNull();
    expect(flow.analysisId).toBeNull();
    expect(flow.profile.other).toEqual({ alias: "" });
    expect(flow.profile.goal_text).toBe("");
    expect(flow.profile.stage_confirmed).toBeUndefined();
    expect(flow.profile.me.alias).toBe("本人");
  });

  it("does not persist personal metadata or reuse legacy stored people", async () => {
    saved.set("rapport_flow", JSON.stringify({ relationshipId: "a", profile: {
      me: { alias: "私有姓名" }, other: { alias: "旧好友", mbti: "INTJ" }, goal: "repair", goal_text: "私有正文",
    } }));
    const { setFlow, getFlowSnapshot } = await import("./store");
    expect(getFlowSnapshot().profile.other).toEqual({ alias: "" });
    setFlow({ profile: { me: { alias: "私有姓名" }, other: { alias: "新好友" }, goal: "repair", goal_text: "私有正文" } });
    const persisted = saved.get("rapport_flow")!;
    expect(persisted).not.toContain("私有");
    expect(JSON.parse(persisted).profile).toBeUndefined();
  });

  it("accepts the newly selected friend's durable profile without old aliases", async () => {
    const { setFlow, getFlowSnapshot } = await import("./store");
    setFlow({ relationshipId: "a" });
    setFlow({ relationshipId: "b", profile: { me: { alias: "本人" }, other: { alias: "朋友 B", revision: 3 }, goal: "understand", goal_text: "" } });
    expect(getFlowSnapshot().profile.other).toEqual({ alias: "朋友 B", revision: 3 });
  });
});
