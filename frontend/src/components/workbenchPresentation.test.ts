import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { WorkbenchReplyDrafts, type WithheldDraft } from "./workbenchReplyDrafts";
import { WorkbenchResearch, type ResearchRun } from "./workbenchResearch";

describe("human reply review shows all usable drafts", () => {
  it("renders every draft and withheld reasons without leaking withheld text", () => {
    const withheld: WithheldDraft[] = JSON.parse('[{"style":"direct","reason":"缺少事实证据","text":"HIDDEN_FACT_DRAFT"}]');
    const html = renderToStaticMarkup(createElement(WorkbenchReplyDrafts, { actualReply: "真实回复", baseReply: "legacy base", candidateReply: "legacy candidate", baseDrafts: [{ style: "casual", text: "微调前整组" }], candidateDrafts: [{ style: "casual", text: "可用候选一" }, { style: "sincere", text: "可用候选二", needs_review: true, risk_notes: ["需要核对语气"] }], withheldDrafts: withheld }));
    for (const expected of ["可用候选一", "可用候选二", "微调前整组", "需要核对语气", "缺少事实证据", "本组拦截了 1 条草稿"]) expect(html).toContain(expected);
    for (const hidden of ["HIDDEN_FACT_DRAFT", "legacy base", "legacy candidate"]) expect(html).not.toContain(hidden);
    expect(html).toContain("不代表原始生成没有事实风险");
  });
  it("does not fall back to raw legacy text when every structured draft was withheld", () => {
    const html = renderToStaticMarkup(createElement(WorkbenchReplyDrafts, { actualReply: "真实", baseReply: "旧", candidateReply: "UNSAFE_LEGACY", candidateDrafts: [], withheldDrafts: [{ style: "direct", reason: "未支持的经历" }] }));
    expect(html).toContain("没有通过检查的可用候选");
    expect(html).not.toContain("UNSAFE_LEGACY");
  });
});

describe("research scopes remain separate", () => {
  it("shows full test, same-ID pilot and calibration as distinct scopes", () => {
    const run: ResearchRun = {
      id: "run1", status: "completed", dataset_id: "data1", data: { counts: { train: 1200, validation: 200, test: 400 }, visibility: "causal", split_protocol: "friend" },
      experiments: [{ method: "dag_erc", architecture: "shared", test: { macro_f1: .4, samples: 400 }, paper_score_reproduction: false }],
      llm_baseline: { model: "local", samples: 32, macro_f1: .2, same_full_test_set: false, aligned_neural_pilot: [{ method: "dag_erc", architecture: "shared", samples: 32, macro_f1: .5 }] },
      calibration: { results: [{ architecture: "shared", validation_samples: 200, test_samples: 400, tasks: { emotion: { fit: { temperature: 1.2, fit_samples: 200, fit_split: "validation" }, test_before: { labelwise_ece: .2, brier: .3 }, test_after: { labelwise_ece: .1, brier: .2 }, rejection_after: { points: [{ threshold: .9, coverage: 0, accepted: 0, top1_set_error: null }] } } } }] },
    };
    const html = renderToStaticMarkup(createElement(WorkbenchResearch, { runs: [run] }));
    for (const expected of ["同一批 32 条消息的小规模对照", "置信度校准", "40.0%", "50.0%", "0.2000", "0.1000", "不能与上表直接判定优劣", "尚未复现原论文成绩", "不是整组多标签判断的正确率"]) expect(html).toContain(expected);
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("undefined");
  });
});
