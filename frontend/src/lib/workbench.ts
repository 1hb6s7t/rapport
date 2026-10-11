import { useEffect, useRef, useState } from "react";
import { ApiError, getCsrf } from "@/api/client";

/** A tab owns its requests; pending work must not populate a different tab. */
export function useWorkbenchAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const lock = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function run(label: string, action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(label);
    setError(null);
    try { await action(); }
    catch (err) { if (alive.current) setError(err); }
    finally { lock.current = false; if (alive.current) setBusy(null); }
  }
  return { busy, error, setError, run };
}

export function formatDate(value?: string | number | null) {
  if (!value) return "尚未记录";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

export async function downloadWorkbench(path: string, body: unknown, filename: string) {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-rapport-csrf": getCsrf() ?? "" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(response.status, data.error?.code ?? "download_failed", data.error?.msg ?? "下载未完成", data.error?.hint ?? "");
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const WORKBENCH_STATUS: Record<string, string> = {
  audited: "已检查", built: "已整理", queued: "等待开始", pending: "等待开始",
  running: "运行中", completed: "已完成", succeeded: "已完成", failed: "失败",
  cancelled: "已取消", cancelling: "正在停止", interrupted: "已中断",
  ready: "可用", candidate: "待评测", evaluated: "已评测", active: "使用中",
  retired: "已停用", blocked: "需处理", rejected: "未通过", trained: "已训练",
  revoked: "来源已撤销",
};

export const MODEL_GATE_REASONS: Record<string, string> = {
  reply_coverage_below_90_percent: "安全候选覆盖率不足 90%，资料不足时会明确拒答",
  reply_alternatives_not_distinct: "至少两种不同回复的覆盖率不足 90%",
  reply_generation_truncated: "仍有回复到达长度上限，需要进一步优化",
  reply_safe_candidates_insufficient: "部分场景缺少足够的安全候选",
  reply_facts_need_review: "生成过缺少本人事实依据的内容，需要复核",
  test_samples_below_30: "独立测试样本不足 30 条",
  macro_f1_below_0_45: "分类效果尚未达到启用标准",
  "macro_f1_below_0.45": "分类效果尚未达到启用标准",
  no_improvement_over_majority_baseline: "未稳定优于简单基线",
  accuracy_improvement_uncertain: "准确性提升尚不稳定",
  production_baseline_comparison_pending: "尚未完成与现有分析方案的同条件对比",
  causal_human_review_required: "实时判断样本仍需人工复核",
  test_evaluation_required: "等待独立测试",
  generation_evaluation_required: "等待实际回复评测",
  adapter_runtime_not_registered: "等待验证本地推理环境",
  heldout_loss_did_not_improve: "独立测试中的回复损失未改善",
  local_latency_over_30_seconds: "本地首次生成耗时超过 30 秒",
  reply_group_latency_over_30_seconds: "整组回复耗时超过 30 秒",
  peak_vram_over_7_gib: "显存峰值超过 7GB 预算",
  peak_vram_over_budget: "显存峰值超过本机预算（显卡总显存减 5GB，至少 7GB）",
  empty_candidate_reply: "测试中出现空回复",
  structured_reply_validation_failed: "回复输出格式或事实检查未通过",
  personal_generation_review_required: "请逐条复核新旧回复样例",
  personal_generation_review_failed: "回复样例复核通过率不足 90%",
  dataset_policy_stale: "模型使用旧的数据保护规则，请从新快照重新训练",
  source_deleted: "模型所用的资料已删除",
  relationship_deleted: "相关好友资料已删除",
};

export function finiteMetric(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function modelMetricSummary(task: string, metrics?: Record<string, unknown>): string {
  const test = metrics?.test as Record<string, unknown> | undefined;
  if (!test) return "尚无独立测试结果";
  if (task === "reply") {
    const loss = finiteMetric(test.reply_loss), base = finiteMetric(test.base_reply_loss);
    return loss === null ? "尚无回复损失结果" : `回复损失 ${loss.toFixed(2)}${base === null ? "" : `（微调前 ${base.toFixed(2)}）`}`;
  }
  const f1 = finiteMetric(test.macro_f1);
  return f1 === null ? "尚无分类结果" : `综合 F1 ${(f1 * 100).toFixed(1)}%`;
}

export function jobDisplayStatus(job: { status: string; cancel_requested?: boolean }) {
  return job.cancel_requested && ["running", "queued"].includes(job.status) ? "cancelling" : job.status;
}

export function compatibleModelComparison(models: { task: string; pool?: string; dataset_id: string; dataset_hash?: string }[]) {
  const first = models[0];
  return models.length >= 2 && models.every(model => model.task === first.task && model.pool === first.pool && model.dataset_id === first.dataset_id && (!first.dataset_hash || !model.dataset_hash || model.dataset_hash === first.dataset_hash));
}

export function scheduleValidation(config: { enabled: boolean; dataset_id: string | null; pool: string; base_model_id?: string | null; min_new_samples: number; source_mode?: string; relationship_ids?: string[] }, available: { datasetIds: string[]; readyBaseIds: string[]; loraReady: boolean; relationshipIds?: string[] }) {
  if (!Number.isInteger(config.min_new_samples) || config.min_new_samples < 200 || config.min_new_samples > 100000) return "新增样本门槛须为 200–100000 的整数。";
  if (!config.enabled) return null;
  if (config.source_mode === "archives") {
    if (!config.relationship_ids?.length || config.relationship_ids.some(id => !available.relationshipIds?.includes(id))) return "请明确选择可用的好友档案。";
    if (config.pool !== "personal_reply") return "长期档案目前用于学习本人表达，请切换任务用途。";
  } else if (!config.dataset_id || !available.datasetIds.includes(config.dataset_id)) return "请选择仍可用的已整理资料。";
  if (config.pool === "personal_reply") {
    if (!available.loraReady) return "个人微调环境尚未准备好。";
    if (!config.base_model_id || !available.readyBaseIds.includes(config.base_model_id)) return "请选择本机已准备好的基础模型。";
  }
  return null;
}
