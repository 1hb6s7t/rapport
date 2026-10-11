import { useState } from "react";
import { Alert, Badge, Card, EmptyState, Field, StatTile } from "@/components/ui";
import { finiteMetric, formatDate, WORKBENCH_STATUS } from "@/lib/workbench";

type ClassScore = { precision?: number; recall?: number; f1?: number; support?: number };
type TaskScore = { macro_f1?: number; micro_f1?: number; per_class?: Record<string, ClassScore>; calibrated?: boolean; labelwise_ece?: number };
type Scores = { macro_f1?: number; tasks?: Record<string, TaskScore>; samples?: number; context_head_latency_ms_p95?: number; latency_excludes_frozen_encoding?: boolean };
type PilotScore = { method: string; architecture: string; seed?: number; samples?: number; macro_f1?: number; tasks?: Record<string, TaskScore> };
type ReliabilityBin = { lower: number; upper: number; count: number; mean_probability: number; positive_frequency: number };
type CalibrationMetrics = { labelwise_ece?: number; macro_label_ece?: number; brier?: number; binary_nll?: number; samples?: number; reliability_bins?: ReliabilityBin[] };
type RejectionCurve = { metric?: string; full_multilabel_correctness?: boolean; points?: { threshold: number; coverage: number; accepted: number; top1_set_error: number | null }[] };
type CalibrationTask = { fit?: { temperature?: number; fit_samples?: number; fit_split?: string }; test_before?: CalibrationMetrics; test_after?: CalibrationMetrics; rejection_before?: RejectionCurve; rejection_after?: RejectionCurve };
type CalibrationResult = { run_id?: string; method?: string; architecture: string; seed?: number; tasks: Record<string, CalibrationTask>; validation_samples?: number; test_samples?: number; ids_hash?: string; production_enabled?: boolean; limitations?: string[] };
export interface ResearchRun {
  id: string; status: string; updated_at?: string | number; dataset_id: string; snapshot_sha256?: string;
  data: { counts?: Record<string, number>; visibility?: string; split_protocol?: string; ids_hash?: string; labels?: string; production_eligible?: boolean; post_hoc_labels?: number };
  config?: { epochs?: number; seed?: number; device?: string; max_context?: number; hidden?: number; batch_size?: number };
  experiments: { method: string; architecture: string; status?: string; error_type?: string; validation?: Scores; test?: Scores; seed?: number; train_seconds?: number; trainable_parameters?: number; paper_score_reproduction?: boolean; production_enabled?: boolean }[];
  limitations?: string[];
  llm_baseline?: { method?: string; model?: string; samples?: number; same_full_test_set?: boolean; test_ids_hash?: string; tasks?: Record<string, TaskScore>; macro_f1?: number; format_failure_rate?: number; latency_seconds_p95?: number; comparison_caveat?: string; input_visibility?: string; aligned_neural_pilot?: PilotScore[] };
  calibration?: { results?: CalibrationResult[] };
}
const METHODS: Record<string, string> = { utterance_mlp: "单句分类对照", dag_erc: "DAG-ERC 思路适配", dialogue_gcn: "DialogueGCN 思路适配", dialogue_crn: "DialogueCRN 思路适配", cosmic: "COSMIC 思路适配", dialogxl: "DialogXL 思路适配" };
const ARCH: Record<string, string> = { shared: "共享多任务", separate: "分任务模型" };
const TASK: Record<string, string> = { emotion: "情绪", intent: "沟通意图" };
function percent(value: unknown) { const n = finiteMetric(value); return n === null ? "—" : `${(n * 100).toFixed(1)}%`; }
function numeric(value: unknown, suffix: string) { const n = finiteMetric(value); return n === null ? "—" : `${n.toFixed(2)} ${suffix}`; }
function decimal(value: unknown) { const n = finiteMetric(value); return n === null ? "—" : n.toFixed(4); }

function AlignedPilot({ baseline }: { baseline: NonNullable<ResearchRun["llm_baseline"]> }) {
  const comparisons = baseline.aligned_neural_pilot;
  if (!comparisons?.length) return null;
  return <section className="space-y-3 rounded-2xl border border-brand-soft bg-brand-mist/50 p-4">
    <h3 className="text-sm font-medium">同一批 {baseline.samples ?? "未记录数量的"} 条消息的小规模对照</h3>
    <p className="text-xs leading-relaxed text-ink-3">下面神经模型的成绩已按大语言模型使用的同一组消息重新计算，仅在本表内比较。它们不替代上方完整测试集的成绩；样本少，结果仍需扩大验证。</p>
    <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-line"><th className="p-2">方法／结构</th><th className="p-2">样本量</th><th className="p-2">综合 F1</th><th className="p-2">情绪 F1</th><th className="p-2">意图 F1</th></tr></thead><tbody>
      <tr className="border-b border-line"><th className="p-2 font-normal">大语言模型 · {baseline.model ?? "未注明"}</th><td className="p-2">{baseline.samples ?? "—"}</td><td className="p-2">{percent(baseline.macro_f1)}</td><td className="p-2">{percent(baseline.tasks?.emotion?.macro_f1)}</td><td className="p-2">{percent(baseline.tasks?.intent?.macro_f1)}</td></tr>
      {comparisons.map((item, index) => <tr className="border-b border-line" key={`${item.method}-${item.architecture}-${item.seed}-${index}`}><th className="p-2 font-normal">{METHODS[item.method] ?? item.method} · {ARCH[item.architecture] ?? item.architecture}</th><td className="p-2">{item.samples ?? "—"}</td><td className="p-2">{percent(item.macro_f1)}</td><td className="p-2">{percent(item.tasks?.emotion?.macro_f1)}</td><td className="p-2">{percent(item.tasks?.intent?.macro_f1)}</td></tr>)}
    </tbody></table></div>
    <p className="text-xs text-ink-3">同 ID 不代表训练条件相同：本地大语言模型使用现有提示，神经判断模型使用了本次实验的训练资料。</p>
    <details className="text-xs text-ink-3"><summary className="cursor-pointer">查看同样本对照的逐类结果</summary><div className="mt-3 space-y-4">{comparisons.map((item, index) => <details key={`${item.method}-${index}`}><summary className="cursor-pointer text-brand-ink">{METHODS[item.method] ?? item.method} · {ARCH[item.architecture] ?? item.architecture}</summary><div className="mt-3"><TaskMetrics tasks={item.tasks} /></div></details>)}</div></details>
  </section>;
}

function CalibrationDetails({ before, after, curveBefore, curveAfter }: { before?: CalibrationMetrics; after?: CalibrationMetrics; curveBefore?: RejectionCurve; curveAfter?: RejectionCurve }) {
  return <div className="space-y-3">
    <div className="overflow-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-line"><th className="p-2">指标（越低越好）</th><th className="p-2">校准前</th><th className="p-2">校准后</th></tr></thead><tbody>{([{ key: "labelwise_ece", label: "概率与实际频率差距 ECE" }, { key: "macro_label_ece", label: "按类别平均的 ECE" }, { key: "brier", label: "概率预测误差 Brier" }, { key: "binary_nll", label: "概率损失" }] as const).map(({ key, label }) => <tr key={key} className="border-b border-line"><th className="p-2 font-normal">{label}</th><td className="p-2 tabular-nums">{decimal(before?.[key])}</td><td className="p-2 tabular-nums">{decimal(after?.[key])}</td></tr>)}</tbody></table></div>
    <details className="text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看概率分段与实际出现频率</summary><div className="mt-3 grid gap-4 sm:grid-cols-2">{[{ label: "校准前", data: before }, { label: "校准后", data: after }].map(({ label, data }) => <div key={label} className="overflow-auto"><h5 className="mb-2 font-medium">{label}</h5><table className="w-full text-left"><thead><tr className="border-b border-line"><th className="p-1">概率段</th><th className="p-1">预测均值</th><th className="p-1">实际频率</th><th className="p-1">数量</th></tr></thead><tbody>{data?.reliability_bins?.map(bin => <tr key={bin.lower} className="border-b border-line"><td className="p-1">{percent(bin.lower)}–{percent(bin.upper)}</td><td className="p-1">{percent(bin.mean_probability)}</td><td className="p-1">{percent(bin.positive_frequency)}</td><td className="p-1">{bin.count}</td></tr>)}</tbody></table></div>)}</div></details>
    <details className="text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看提高置信门槛后，保留多少判断</summary><p className="mt-2 leading-relaxed">这里统计最高概率标签是否包含在标注答案中，不是整组多标签判断的正确率。覆盖率越低，表示更多样本被暂缓判断；没有保留样本时错误率为空。</p><div className="mt-3 overflow-auto"><table className="w-full text-left"><thead><tr className="border-b border-line"><th className="p-2">固定门槛</th><th className="p-2">校准前覆盖率</th><th className="p-2">校准后覆盖率</th><th className="p-2">前／后错误率</th><th className="p-2">前／后保留数</th></tr></thead><tbody>{(curveAfter?.points ?? []).map(point => { const old = curveBefore?.points?.find(item => item.threshold === point.threshold); return <tr className="border-b border-line" key={point.threshold}><td className="p-2">{percent(point.threshold)}</td><td className="p-2">{percent(old?.coverage)}</td><td className="p-2">{percent(point.coverage)}</td><td className="p-2">{percent(old?.top1_set_error)} / {percent(point.top1_set_error)}</td><td className="p-2">{old?.accepted ?? "—"} / {point.accepted}</td></tr>; })}</tbody></table></div></details>
  </div>;
}

function CalibrationSection({ results }: { results?: CalibrationResult[] }) {
  if (!results?.length) return null;
  return <section className="space-y-3 rounded-2xl border border-line bg-paper/70 p-4"><h3 className="text-sm font-medium">置信度校准 · 独立查看前后变化</h3><p className="text-xs leading-relaxed text-ink-3">温度参数只由验证集拟合，再在测试集测量概率误差。改善概率可靠性不等于提高所有类别的识别率，也不能替代人工金标准验证。</p>
    {results.map((result, index) => <details key={`${result.architecture}-${result.seed}-${index}`} className="rounded-xl border border-line p-3"><summary className="cursor-pointer text-sm text-brand-ink">DAG-ERC · {ARCH[result.architecture] ?? result.architecture} · 验证 {result.validation_samples ?? "—"} 条／测试 {result.test_samples ?? "—"} 条</summary><div className="mt-3 space-y-4">
      {Object.entries(result.tasks).map(([task, data]) => <section key={task} className="space-y-2"><h4 className="text-sm font-medium">{TASK[task] ?? task}</h4><p className="text-xs text-ink-3">温度 {decimal(data.fit?.temperature)}，使用 {data.fit?.fit_samples ?? "—"} 条{data.fit?.fit_split === "validation" ? "验证" : "未注明分区"}样本拟合；以下分数来自测试集。</p><CalibrationDetails before={data.test_before} after={data.test_after} curveBefore={data.rejection_before} curveAfter={data.rejection_after} /></section>)}
      {result.limitations?.length ? <ul className="list-disc space-y-1 pl-5 text-xs text-ink-3">{result.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul> : null}
      <p className="text-xs text-ink-3">{result.production_enabled ? "此校准已标记接入运行。" : "此校准尚未自动接入现有分析。"}它使用本实验的完整测试范围，与上方小规模同 ID 对照分别解释。</p>
    </div></details>)}
  </section>;
}

function TaskMetrics({ tasks }: { tasks?: Record<string, TaskScore> }) {
  if (!tasks || Object.keys(tasks).length === 0) return <p className="text-xs text-ink-3">暂无逐类指标。</p>;
  return <div className="space-y-4">{Object.entries(tasks).map(([key, task]) => <section key={key}>
    <h4 className="text-sm font-medium">{TASK[key] ?? key} · 综合 F1 {percent(task.macro_f1)}</h4>
    {task.calibrated === false && <p className="my-2 text-xs text-ink-3">置信度尚未校准，不能把模型分数解释成真实准确率。</p>}
    <div className="overflow-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-line"><th className="p-2">类别</th><th className="p-2">精确率</th><th className="p-2">召回率</th><th className="p-2">F1</th><th className="p-2">样本量</th></tr></thead><tbody>{Object.entries(task.per_class ?? {}).map(([label, value]) => <tr key={label} className="border-b border-line"><th className="p-2 font-normal">{label}</th><td className="p-2">{percent(value.precision)}</td><td className="p-2">{percent(value.recall)}</td><td className="p-2">{percent(value.f1)}</td><td className="p-2">{finiteMetric(value.support) ?? "—"}</td></tr>)}</tbody></table></div>
  </section>)}</div>;
}

export function WorkbenchResearch({ runs }: { runs: ResearchRun[] }) {
  const [selected, setSelected] = useState("");
  const run = runs.find(item => item.id === selected) ?? runs[0];
  return <Card title="研究方法在本项目中的实际比较" hint="每次实验单独查看；保留训练范围、测试划分和失败记录，不跨测试集合排总榜。">
    {!run ? <EmptyState title="尚无研究实验结果" hint="训练基线与论文方法复现分开记录；没有结果时不展示模拟分数。" /> : <div className="space-y-4">
      <Field label="选择一次实验"><select className="input" value={run.id} onChange={event => setSelected(event.target.value)}>{runs.map(item => <option key={item.id} value={item.id}>{formatDate(item.updated_at)} · {WORKBENCH_STATUS[item.status] ?? item.status} · {item.id.slice(0, 8)}</option>)}</select></Field>
      <div className="flex flex-wrap gap-2"><Badge tone={run.status === "failed" ? "warn" : "brand"}>{WORKBENCH_STATUS[run.status] ?? run.status}</Badge><Badge tone="lavender">中文场景适配实验</Badge><Badge>资料 {run.dataset_id.slice(0, 8)}</Badge></div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4"><StatTile label="训练样本" value={run.data.counts?.train?.toLocaleString() ?? "—"} /><StatTile label="验证样本" value={run.data.counts?.validation?.toLocaleString() ?? "—"} /><StatTile label="测试样本" value={run.data.counts?.test?.toLocaleString() ?? "—"} /><StatTile label="训练轮数" value={run.config?.epochs ?? "—"} /></div>
      <p className="text-sm leading-relaxed text-ink-2">输入范围：{run.data.visibility === "causal" ? "只使用当时已出现的上下文" : run.data.visibility === "offline" ? "历史复盘，可参考后文" : "未记录"}。数据划分：{run.data.split_protocol === "friend" ? "按好友隔离" : run.data.split_protocol === "time" ? "按时间隔离" : "未记录"}。每条最多 {run.config?.max_context ?? "未记录"} 轮上下文。</p>
      {run.data.labels === "ai_assisted_development_not_human_gold" && <Alert tone="info">本次标签来自 AI 辅助开发资料，不是独立人工金标准。{(run.data.post_hoc_labels ?? 0) > 0 && `其中 ${run.data.post_hoc_labels} 条标签含后续信息，需要与实时判断验证分开解释。`}</Alert>}
      {run.experiments.length === 0 ? <p className="text-sm text-ink-3">实验仍在准备，尚无已记录的方法成绩。</p> : <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b border-line text-xs text-ink-3"><th className="p-2">方法／结构</th><th className="p-2">验证 F1</th><th className="p-2">测试 F1</th><th className="p-2">训练耗时</th><th className="p-2">状态</th></tr></thead><tbody>{run.experiments.map((experiment, index) => <tr key={`${experiment.method}-${experiment.architecture}-${experiment.seed}-${index}`} className="border-b border-line"><th className="p-2 font-normal"><span className="block">{METHODS[experiment.method] ?? experiment.method}</span><span className="text-xs text-ink-3">{ARCH[experiment.architecture] ?? experiment.architecture}</span></th><td className="p-2 tabular-nums">{percent(experiment.validation?.macro_f1)}</td><td className="p-2 tabular-nums">{percent(experiment.test?.macro_f1)}</td><td className="p-2 tabular-nums">{numeric(experiment.train_seconds, "秒")}</td><td className="p-2 text-xs">{experiment.status === "failed" ? `失败：${experiment.error_type ?? "未注明"}` : experiment.test ? "有独立测试记录" : WORKBENCH_STATUS[experiment.status ?? ""] ?? "等待结果"}</td></tr>)}</tbody></table></div>}
      <p className="text-xs text-ink-3">F1 只在相同任务和测试样本上比较。这里保持实验原有顺序，不根据单次最高分直接启用模型。</p>
      {run.experiments.map((experiment, index) => <details key={`${experiment.method}-${index}`} className="rounded-2xl border border-line p-4"><summary className="cursor-pointer text-sm text-brand-ink">{METHODS[experiment.method] ?? experiment.method} · {ARCH[experiment.architecture] ?? experiment.architecture} · 逐类表现与运行范围</summary><div className="mt-3 space-y-3"><p className="text-xs text-ink-3">可训练参数 {experiment.trainable_parameters?.toLocaleString() ?? "未记录"}。上下文判断部分 p95：{numeric(experiment.test?.context_head_latency_ms_p95, "毫秒")}。{experiment.test?.latency_excludes_frozen_encoding && "该耗时不含文本编码，不能代表整套系统速度。"}</p><p className="text-xs text-ink-3">{experiment.paper_score_reproduction === true ? "已标记为原论文成绩复现，需结合实验报告核对原数据与设置。" : "这是中文适配实验，尚未复现原论文成绩。"}{experiment.production_enabled === true ? "该结果已接入运行流程。" : "该结果没有直接替换现有分析模型。"}</p><TaskMetrics tasks={experiment.test?.tasks} /></div></details>)}
      {run.llm_baseline && <section className="space-y-3 rounded-2xl border border-line bg-paper/70 p-4"><h3 className="text-sm font-medium">大语言模型对照 · 单独报告</h3><p className="text-xs text-ink-3">模型 {run.llm_baseline.model ?? "未记录"} · {run.llm_baseline.samples ?? "未记录"} 条样本 · F1 {percent(run.llm_baseline.macro_f1)} · p95 {numeric(run.llm_baseline.latency_seconds_p95, "秒")}</p>{!run.llm_baseline.same_full_test_set && <Alert tone="warn">这个对照没有使用相同的完整测试集，不能与上表直接判定优劣。</Alert>}{run.llm_baseline.comparison_caveat && <p className="text-xs text-ink-3">{run.llm_baseline.comparison_caveat}</p>}<details><summary className="cursor-pointer text-xs text-brand-ink">查看对照的逐类结果</summary><div className="mt-3"><TaskMetrics tasks={run.llm_baseline.tasks} /></div></details></section>}
      {run.llm_baseline && <AlignedPilot baseline={run.llm_baseline} />}
      <CalibrationSection results={run.calibration?.results} />
      {run.limitations?.length ? <ul className="list-disc space-y-1 pl-5 text-xs text-ink-3">{run.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul> : null}
      <details className="text-xs text-ink-3"><summary className="cursor-pointer">核对数据与测试标识</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ snapshot: run.snapshot_sha256, examples: run.data.ids_hash, baseline_test: run.llm_baseline?.test_ids_hash, config: run.config }, null, 2)}</pre></details>
    </div>}
  </Card>;
}
