import { describe, expect, it } from "vitest";
import { compatibleModelComparison, finiteMetric, jobDisplayStatus, modelMetricSummary, scheduleValidation } from "./workbench";

describe("workbench quality and job reporting", () => {
  it("does not render absent or non-finite reply metrics as a score", () => {
    expect(modelMetricSummary("reply", { test: { reply_loss: 1.23 } })).toBe("回复损失 1.23");
    expect(modelMetricSummary("reply", { test: { macro_f1: 0.9 } })).toBe("尚无回复损失结果");
    expect(modelMetricSummary("analysis", { test: { macro_f1: NaN } })).toBe("尚无分类结果");
    expect(finiteMetric(Infinity)).toBeNull();
  });
  it("keeps reply loss and classification F1 separate", () => {
    expect(modelMetricSummary("analysis", { test: { macro_f1: 0.412 } })).toBe("综合 F1 41.2%");
    expect(modelMetricSummary("reply", { test: { reply_loss: 1.2, base_reply_loss: 1.9 } })).toBe("回复损失 1.20（微调前 1.90）");
    expect(modelMetricSummary("reply", { validation: { loss: 1 } })).toBe("尚无独立测试结果");
  });
  it("does not claim cancellation completed while the worker is running", () => {
    expect(jobDisplayStatus({ status: "running", cancel_requested: true })).toBe("cancelling");
    expect(jobDisplayStatus({ status: "completed", cancel_requested: true })).toBe("completed");
    expect(jobDisplayStatus({ status: "cancelled", cancel_requested: true })).toBe("cancelled");
  });
  it("rejects comparisons across different tasks, pools or snapshots", () => {
    const baseline = { task: "analysis", pool: "offline_review", dataset_id: "d1", dataset_hash: "h1" };
    expect(compatibleModelComparison([baseline, { ...baseline }])).toBe(true);
    for (const change of [{ task: "reply" }, { pool: "realtime_review" }, { dataset_id: "d2" }, { dataset_hash: "h2" }]) {
      expect(compatibleModelComparison([baseline, { ...baseline, ...change }])).toBe(false);
    }
    expect(compatibleModelComparison([])).toBe(false);
  });
});

describe("automatic learning form validation", () => {
  const available = { datasetIds: ["d1"], readyBaseIds: ["local-base"], loraReady: true };
  const schedule = { enabled: true, dataset_id: "d1", pool: "personal_reply", base_model_id: "local-base", min_new_samples: 200 };
  it("requires a present dataset and ready reply base before enabling", () => {
    expect(scheduleValidation(schedule, available)).toBeNull();
    expect(scheduleValidation({ ...schedule, dataset_id: "deleted" }, available)).toContain("资料");
    expect(scheduleValidation({ ...schedule, base_model_id: "removed" }, available)).toContain("基础模型");
    expect(scheduleValidation(schedule, { ...available, loraReady: false })).toContain("微调环境");
  });
  it("permits switching an invalid old schedule off", () => {
    expect(scheduleValidation({ ...schedule, enabled: false, dataset_id: null, base_model_id: null }, available)).toBeNull();
  });
  it("matches the server's inclusive sample threshold limits", () => {
    for (const value of [0, 199, 200.5, 100001, NaN]) expect(scheduleValidation({ ...schedule, min_new_samples: value }, available)).toContain("整数");
    expect(scheduleValidation({ ...schedule, min_new_samples: 100000 }, available)).toBeNull();
  });
  it("allows archive bootstrap without dataset but requires explicit live friends", () => {
    const archive = { ...schedule, source_mode: "archives", dataset_id: null, relationship_ids: ["r1"] };
    expect(scheduleValidation(archive, { ...available, relationshipIds: ["r1"] })).toBeNull();
    expect(scheduleValidation({ ...archive, relationship_ids: [] }, { ...available, relationshipIds: ["r1"] })).toContain("好友");
    expect(scheduleValidation(archive, { ...available, relationshipIds: [] })).toContain("好友");
    expect(scheduleValidation({ ...archive, pool: "offline_review" }, { ...available, relationshipIds: ["r1"] })).toContain("用途");
  });
});
