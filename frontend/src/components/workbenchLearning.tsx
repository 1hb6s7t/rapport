import { useEffect, useState } from "react";
import { CheckCheck, FlaskConical, Pause, Play, RotateCcw, Sprout, Timer } from "lucide-react";
import { api } from "@/api/client";
import { ModelReplyReview } from "@/components/ModelReplyReview";
import { WorkbenchResearch, type ResearchRun } from "@/components/workbenchResearch";
import { WorkbenchInteractionResearch, type InteractionStudy } from "@/components/workbenchInteractionResearch";
import { useRelationships } from "@/components/RelationshipPicker";
import { Alert, Badge, Button, Card, EmptyState, ErrorBox, Field, Modal, Spinner } from "@/components/ui";
import type { DatasetManifest, DatasetPool } from "@/components/workbenchDataset";
import { compatibleModelComparison, finiteMetric, formatDate, jobDisplayStatus, modelMetricSummary, MODEL_GATE_REASONS, scheduleValidation, useWorkbenchAction, WORKBENCH_STATUS } from "@/lib/workbench";

interface LearningJob { id: string; kind: string; status: string; cancel_requested?: boolean; progress: number; stage?: string; error?: unknown; result?: unknown; created_at: string | number; model_id?: string; dataset_id?: string; architecture?: string; retry_of?: string }
interface LearningModel {
  id: string; task: string; pool?: DatasetPool; architecture: string; state: string; dataset_id: string;
  personal_trial_available?: boolean; activation_scope?: string; metrics?: Record<string, unknown>;
  gate?: { passed: boolean; reasons?: string[]; reviewed_samples?: number; accepted_samples?: number; quality_review_source?: string };
  runtime_supported?: boolean; dataset_hash?: string;
}
interface LearningSchedule {
  base_model_id?: string | null; enabled: boolean; dataset_id: string | null; weekly_day: number; min_new_samples: number;
  pool: DatasetPool; architecture: string; last_inspected_at?: string | number | null; last_trained_at?: string | number | null;
  source_mode?: "dataset" | "archives"; relationship_ids?: string[]; timezone?: string;
  last_error?: string | null; last_status?: string; last_new_samples?: number;
  prepare_job_id?: string | null; training_job_id?: string | null;
}
interface LearningView {
  capabilities: { cpu_baseline: boolean; lora_dependencies: boolean; cloud_training: boolean; local_base_models?: { id: string; ready: boolean }[] };
  papers: { id: string; name: string; url: string; replication_status: string }[];
  schedules: LearningSchedule;
  jobs: LearningJob[];
  models: LearningModel[];
  research_runs?: ResearchRun[];
  interaction_studies?: InteractionStudy[];
  personal_runtime?: { status: "unloaded" | "loading" | "ready" | "error"; model_id?: string | null; load_seconds?: number; error?: string | null };
}
const ACTIVE_JOBS = new Set(["queued", "pending", "running", "cancelling"]);
const ARCHITECTURES: Record<string, string> = { shared: "共享判断模型", separate: "分任务判断模型", reply_lora: "个人表达适配器" };
const POOL_LABELS: Record<DatasetPool, string> = { offline_review: "历史复盘", realtime_review: "实时判断（已人工复核）", personal_reply: "本人的表达" };
const JOB_LABELS: Record<string, string> = { dataset_build: "整理样本", archive_prepare: "整理长期档案", train: "训练", evaluate: "评测" };

function useLearningSnapshot(poll = false) {
  const [view, setView] = useState<LearningView | null>(null);
  const [datasets, setDatasets] = useState<DatasetManifest[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    async function pull() {
      try {
        const [snapshot, data] = await Promise.all([api.get<LearningView>("/learning"), api.get<{ items: DatasetManifest[] }>("/datasets")]);
        if (!alive) return;
        setView(snapshot); setDatasets(data.items.filter(item => item.status === "built")); setError(null);
        if (poll && (snapshot.jobs.some(job => ACTIVE_JOBS.has(job.status)) || snapshot.personal_runtime?.status === "loading")) timer = window.setTimeout(pull, 4000);
        else if (poll && snapshot.schedules.enabled) timer = window.setTimeout(pull, 30000);
      } catch (err) { if (alive) { setError(err); if (poll) timer = window.setTimeout(pull, 8000); } }
      finally { if (alive) setLoading(false); }
    }
    void pull();
    return () => { alive = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [revision, poll]);
  return { view, datasets, error, loading, refresh: () => setRevision(value => value + 1) };
}
function score(model: LearningModel): string {
  return modelMetricSummary(model.task, model.metrics);
}

function ModelMetrics({ model }: { model: LearningModel }) {
  const test = model.metrics?.test as Record<string, unknown> | undefined;
  if (!test) return <p className="mt-2 text-xs text-ink-3">训练成绩不能替代独立测试；请先完成评测。</p>;
  const samples = finiteMetric(test.samples);
  const latency = finiteMetric(model.task === "reply" ? test.reply_group_warm_p95_s : test.latency_ms_p95);
  const vram = finiteMetric(test.peak_reserved_vram_bytes);
  return <details className="mt-3 text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看指标说明与测量范围</summary><div className="mt-2 space-y-2 rounded-xl bg-card/60 p-3">
    <p>测试样本：{samples ?? "未记录"}。{model.task === "reply" ? "回复损失越低，说明对独立样本表达的预测更接近；它不能单独证明回复合适或事实正确。" : "综合 F1 同时反映各类别的精确率与召回率，不等于喜欢概率，也不等于逐条判断正确率。"}</p>
    {model.task === "reply" && model.gate?.reviewed_samples != null && <p>质量复核：{model.gate.accepted_samples ?? 0} / {model.gate.reviewed_samples} 组可用；来源：{model.gate.quality_review_source === "delegated_assistant" ? "受托助手逐条复核" : "用户复核"}。低于门槛时不能启用。</p>}
    <p>{model.task === "reply" ? "整组回复预热后 p95" : "单样本分类 p95"}：{latency === null ? "未记录" : `${latency.toFixed(2)} ${model.task === "reply" ? "秒" : "毫秒"}`}。{model.task !== "reply" && "此耗时不代表整套回复流程。"}</p>
    {model.task === "reply" && <><p>模型首次载入：{finiteMetric(test.model_load_s)?.toFixed(2) ?? "未记录"} 秒。显存峰值：{vram === null ? "未记录" : `${(vram / 1024 ** 3).toFixed(2)} GiB`}。</p><p>结构化回复测试：{finiteMetric(test.structured_samples) ?? "未记录"} 组；未通过：{finiteMetric(test.structured_failures) ?? "未记录"} 组。</p><p>格式通过不等于原始生成没有事实风险；拦截的草稿数量与原因、剩余整组候选，请在回复样例复核中逐项查看。</p></>}
    <details><summary className="cursor-pointer">完整逐类指标与来源版本</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ dataset_id: model.dataset_id, dataset_hash: model.dataset_hash, metrics: model.metrics }, null, 2)}</pre></details>
  </div></details>;
}
function JobStatus({ status }: { status: string }) {
  return <Badge tone={status === "completed" || status === "active" ? "mint" : status === "failed" || status === "interrupted" ? "warn" : ACTIVE_JOBS.has(status) ? "brand" : "neutral"}>{WORKBENCH_STATUS[status] ?? status}</Badge>;
}

export function WorkbenchLearning() {
  const { view, datasets, error: loadError, loading, refresh } = useLearningSnapshot(true);
  const [datasetId, setDatasetId] = useState("");
  const [pool, setPool] = useState<DatasetPool>("offline_review");
  const [architecture, setArchitecture] = useState("shared");
  const [baseModel, setBaseModel] = useState("");
  const [epochs, setEpochs] = useState(2);
  const [sampleBudget, setSampleBudget] = useState(128);
  const [trainingPreview, setTrainingPreview] = useState(false);
  const [activation, setActivation] = useState<{ model: LearningModel; rollback: boolean } | null>(null);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [comparison, setComparison] = useState<LearningModel[] | null>(null);
  const [notice, setNotice] = useState("");
  const { busy, error, run } = useWorkbenchAction();
  const effectiveDataset = datasets.some(item => item.id === datasetId) ? datasetId : datasets[0]?.id || "";
  const chosen = datasets.find(item => item.id === effectiveDataset);
  const readyBases = view?.capabilities.local_base_models?.filter(model => model.ready) ?? [];
  const effectiveBase = readyBases.some(item => item.id === baseModel) ? baseModel : readyBases[0]?.id || "";
  const personal = architecture === "reply_lora";
  const canTrain = !!chosen && (chosen.eligible?.[pool] ?? 0) > 0 && (personal ? !!view?.capabilities.lora_dependencies && !!effectiveBase : !!view?.capabilities.cpu_baseline);
  const jobBody = { kind: "train", dataset_id: effectiveDataset, pool, architecture, max_features: 12000, epochs, ...(personal ? { base_model_id: effectiveBase, max_train_samples: sampleBudget || null } : {}) };
  function selectPurpose(value: DatasetPool) { setPool(value); setArchitecture(value === "personal_reply" ? "reply_lora" : "shared"); }
  return <div className="space-y-5">
    <ErrorBox error={error ?? loadError} />{busy && <Spinner label={busy} />}{notice && <Alert tone="good">{notice}</Alert>}
    {loading ? <Spinner label="读取训练与模型版本…" /> : !view ? <Button onClick={refresh}>重新读取</Button> : <>
      {view.models.some(model => model.state === "active" && model.task === "reply") && view.personal_runtime && <Card title="正在使用的个人回复模型" right={<Badge tone={view.personal_runtime.status === "ready" ? "mint" : view.personal_runtime.status === "error" ? "warn" : "brand"}>{{ unloaded: "尚未载入", loading: "预热中", ready: "已就绪", error: "载入失败" }[view.personal_runtime.status]}</Badge>}>
        {view.personal_runtime.status === "loading" ? <Spinner label="个人回复模型正在预热，完成后可减少首次等待…" /> : <p className="text-sm text-ink-2">{view.personal_runtime.status === "ready" ? "已完成本机预热。生成时按需使用显卡，并与 OCR、训练任务协调资源。" : view.personal_runtime.status === "error" ? "个人模型暂时不可用，请查看原因并重新检查运行状态。" : "这个版本已经由你启用，正在等待本机载入。"}</p>}
        {view.personal_runtime.error && <div className="mt-3"><Alert tone="warn">{view.personal_runtime.error}</Alert></div>}
        <p className="mt-2 text-xs text-ink-3">版本 {view.personal_runtime.model_id?.slice(0, 8) ?? "待确认"}{finiteMetric(view.personal_runtime.load_seconds) !== null ? ` · 最近载入 ${view.personal_runtime.load_seconds?.toFixed(1)} 秒` : ""}</p>
      </Card>}
      <Card title="先做比较，再决定启用" hint="新训练的版本不会自动替换正在使用的模型。" right={<FlaskConical size={22} className="text-brand" />}>
        <div className="flex flex-wrap gap-2"><Badge tone={view.capabilities.cpu_baseline ? "mint" : "neutral"}>本机基线实验 {view.capabilities.cpu_baseline ? "可用" : "未就绪"}</Badge><Badge tone={view.capabilities.lora_dependencies ? "mint" : "warn"}>个人微调环境 {view.capabilities.lora_dependencies ? "已安装" : "待准备"}</Badge><Badge tone="neutral">云端训练 {view.capabilities.cloud_training ? "可用" : "未接入"}</Badge></div>
        <p className="mt-3 text-sm leading-relaxed text-ink-2">共享与分任务方案先使用同一份资料进行本地基线比较。基线结果会单独记录，不记作论文复现成绩；个人回复训练学习的是你实际发出的表达。</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label="资料版本"><select className="input" value={effectiveDataset} disabled={!!busy || datasets.length === 0} onChange={event => setDatasetId(event.target.value)}>{datasets.length === 0 && <option value="">先到「数据与复核」整理样本</option>}{datasets.map(item => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {item.id.slice(0, 8)}</option>)}</select></Field>
          <Field label="这次学习什么"><select className="input" value={pool} disabled={!!busy} onChange={event => selectPurpose(event.target.value as DatasetPool)}>{Object.entries(POOL_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></Field>
          {!personal ? <Field label="比较的方案"><select className="input" value={architecture} disabled={!!busy} onChange={event => setArchitecture(event.target.value)}><option value="shared">共享判断模型 · 情绪与意图一起学习</option><option value="separate">分任务判断模型 · 分别学习后比较</option></select></Field> : <Field label="本机已准备的基础模型"><select className="input" value={effectiveBase} disabled={!!busy || readyBases.length === 0} onChange={event => setBaseModel(event.target.value)}>{readyBases.length === 0 && <option value="">尚未准备可训练模型</option>}{readyBases.map(model => <option key={model.id} value={model.id}>{model.id}</option>)}</select></Field>}
          <Field label="训练轮数" hint="先少量试跑；新版本仍需独立评测。"><input className="input" type="number" min={1} max={10} value={epochs} disabled={!!busy} onChange={event => setEpochs(Number(event.target.value))} /></Field>
        </div>
        {personal && <div className="mt-3"><Field label="本次训练样本预算"><select className="input" value={sampleBudget} disabled={!!busy} onChange={event => setSampleBudget(Number(event.target.value))}><option value={128}>先试跑 128 条合格样本</option><option value={512}>512 条合格样本</option><option value={0}>全部合格训练样本</option></select></Field></div>}
        {personal && readyBases.length === 0 && <div className="mt-3"><Alert tone="warn">本机尚未准备可训练的基础模型。已有的 Ollama 聊天模型不能直接作为此处的微调文件。</Alert></div>}
        {personal && !view.capabilities.lora_dependencies && <p className="mt-2 text-xs text-ink-3">需要先安装可用的本地微调环境；不会自动下载模型或把聊天发到云端。</p>}
        {chosen && (chosen.eligible?.[pool] ?? 0) === 0 && <div className="mt-3"><Alert tone="warn">所选分组没有符合训练条件的样本，请先整理或完成人工复核。</Alert></div>}{pool === "realtime_review" && <p className="mt-3 text-xs text-ink-3">仅使用已人工核对的实时样本。自动标签质量较高，也不等于已经过因果复核。</p>}
        <div className="mt-4 flex flex-wrap gap-2"><Button variant="primary" disabled={!!busy || !canTrain || !Number.isInteger(epochs) || epochs < 1 || epochs > 10} onClick={() => setTrainingPreview(true)}><Sprout size={16} />预览本次训练</Button><Button disabled={!!busy} onClick={refresh}>刷新状态</Button></div>
      </Card>
      <Card title="训练与评测进度" hint="关闭本页不会取消已提交的任务。正在运行时，进度每隔几秒更新。">
        {view.jobs.length === 0 ? <EmptyState title="还没有训练或评测任务" hint="从上面选择资料和方案，先查看预览。" /> : <div className="space-y-3">{view.jobs.map(job => <article key={job.id} className="rounded-2xl border border-line bg-paper/70 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-medium">{JOB_LABELS[job.kind] ?? job.kind}</h3><JobStatus status={jobDisplayStatus(job)} /><span className="text-xs text-ink-3">{formatDate(job.created_at)}</span></div><div className="flex gap-2">{ACTIVE_JOBS.has(job.status) && <Button size="sm" disabled={!!busy || jobDisplayStatus(job) === "cancelling"} onClick={() => void run("正在请求停止…", async () => { await api.post(`/learning/jobs/${encodeURIComponent(job.id)}/cancel`); refresh(); })}><Pause size={13} />{job.cancel_requested ? "等待安全停止" : "停止"}</Button>}{["failed", "cancelled", "interrupted"].includes(job.status) && <Button size="sm" disabled={!!busy} onClick={() => void run("正在重新提交…", async () => { await api.post(`/learning/jobs/${encodeURIComponent(job.id)}/retry`); refresh(); })}><RotateCcw size={13} />重试原任务</Button>}</div></div>{ACTIVE_JOBS.has(job.status) && <progress className="mt-3 h-2 w-full accent-[var(--color-brand)]" value={Math.min(1, Math.max(0, job.progress))} max={1} aria-label="任务进度" />}{job.cancel_requested && ACTIVE_JOBS.has(job.status) && <p className="mt-2 text-xs text-warn">停止请求已记录，正在等待当前步骤结束；任务尚未停止。</p>}{job.retry_of && <p className="mt-2 text-xs text-ink-3">重试自 {job.retry_of.slice(0, 8)}，沿用原任务资料与设置。</p>}{job.stage && <p className="mt-2 text-xs text-ink-3">{job.stage}</p>}{job.error != null && <p className="mt-2 break-words text-sm text-warn">{typeof job.error === "string" ? job.error : JSON.stringify(job.error)}</p>}{job.result != null && <details className="mt-2 text-xs text-ink-3"><summary className="cursor-pointer text-brand-ink">查看任务结果</summary><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(job.result, null, 2)}</pre></details>}</article>)}</div>}
      </Card>
      <Card title="模型版本" hint="个人回复版本须完成独立评测和逐条质量复核，达标后才能正式启用；本机试用仍需自动检查通过并由你确认。" right={<Button size="sm" disabled={!!busy || selectedModels.length < 2 || selectedModels.length > 10} onClick={() => void run("读取版本对比…", async () => { const result = await api.get<{ models: LearningModel[] }>(`/learning/models/compare?ids=${selectedModels.map(encodeURIComponent).join(",")}`); setComparison(result.models); })}>比较所选版本</Button>}>
        {view.models.length === 0 ? <EmptyState title="还没有模型产物" hint="训练完成后，候选版本会出现在这里。" /> : <div className="space-y-3">{view.models.map(model => <article key={model.id} className="rounded-2xl border border-line bg-paper/70 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><label className="flex min-w-0 items-center gap-2 text-sm"><input type="checkbox" checked={selectedModels.includes(model.id)} aria-label={`选择版本 ${model.id.slice(0, 8)} 比较`} disabled={!selectedModels.includes(model.id) && selectedModels.length >= 10} onChange={event => setSelectedModels(old => event.target.checked ? [...old, model.id] : old.filter(id => id !== model.id))} />{ARCHITECTURES[model.architecture] ?? model.architecture} · {model.id.slice(0, 8)}</label><JobStatus status={model.state} /></div><div className="mt-3 grid gap-2 text-xs text-ink-3 sm:grid-cols-3"><p>任务：{POOL_LABELS[model.pool as DatasetPool] ?? model.task}</p><p>测试结果：{score(model)}</p><p>启用检查：{model.gate?.passed ? "已通过" : model.personal_trial_available ? "自动检查通过，人工质量未评" : "尚未通过"}</p></div>{model.gate?.reasons?.length ? <ul className="mt-2 list-disc pl-5 text-xs text-warn">{model.gate.reasons.map((reason, i) => <li key={i}>{MODEL_GATE_REASONS[reason] ?? reason}</li>)}</ul> : null}{model.runtime_supported === false && <p className="mt-2 text-xs text-ink-3">当前产物用于实验比较，尚不支持直接替换运行模型。</p>}<ModelMetrics model={model} /><div className="mt-3 flex flex-wrap gap-2"><Button size="sm" disabled={!!busy || model.state === "revoked" || view.jobs.some(job => job.model_id === model.id && ACTIVE_JOBS.has(job.status))} onClick={() => void run("正在提交评测…", async () => { await api.post("/learning/jobs", { kind: "evaluate", model_id: model.id, dataset_id: model.dataset_id }); refresh(); })}><FlaskConical size={13} />评测此版本</Button>{model.state !== "active" && <Button size="sm" variant="soft" disabled={!!busy || (!model.gate?.passed && !model.personal_trial_available) || !model.runtime_supported} onClick={() => setActivation({ model, rollback: false })}><CheckCheck size={13} />{model.personal_trial_available && !model.gate?.passed ? "确认本机试用" : "启用此版本"}</Button>}{model.state === "active" && <Button size="sm" disabled={!!busy} onClick={() => void run("正在停用模型…", async () => { await api.post(`/learning/models/${encodeURIComponent(model.id)}/deactivate`); refresh(); })}>停用此版本</Button>}{model.state !== "active" && view.models.some(item => item.state === "active" && item.task === model.task && item.pool === model.pool) && <Button size="sm" disabled={!!busy || (!model.gate?.passed && !model.personal_trial_available) || !model.runtime_supported} onClick={() => setActivation({ model, rollback: true })}><RotateCcw size={13} />回到此版本</Button>}</div></article>)}</div>}
      </Card>
      <div className="flex flex-wrap gap-2">{view.models.filter(model => model.task === "reply" && model.metrics?.test && model.state !== "revoked").map(model => <ModelReplyReview key={model.id} modelId={model.id} onReviewed={refresh} />)}</div>
      <WorkbenchResearch runs={view.research_runs ?? []} />
      <WorkbenchInteractionResearch studies={view.interaction_studies ?? []} />
      <Card title="研究方法的真实进度" hint="论文参考、方法适配和原实验复现分别记录。"><div className="grid gap-3 sm:grid-cols-2">{view.papers.map(paper => <div key={paper.id} className="rounded-xl border border-line p-3"><a href={paper.url} target="_blank" rel="noreferrer" className="text-sm font-medium">{paper.name}</a><p className="mt-1 text-xs text-ink-3">{({ not_started: "尚未开始原实验复现", planned: "待复现", reference_only: "已收录方法参考", not_reproduced: "尚未复现", reproduced: "已记录复现结果" } as Record<string, string>)[paper.replication_status] ?? paper.replication_status}</p></div>)}</div></Card>
    </>}
    <Modal open={trainingPreview} onClose={() => { if (!busy) setTrainingPreview(false); }} title="确认本次本地训练"><div className="space-y-4"><p className="text-sm text-ink-2">这次使用 <strong>{POOL_LABELS[pool]}</strong>，训练 <strong>{ARCHITECTURES[architecture]}</strong>，共 {epochs} 轮。资料版本 {effectiveDataset.slice(0, 8)}。</p><p className="text-sm text-ink-2">所选样本组共 {chosen?.pools?.[pool]?.toLocaleString() ?? "—"} 条，实际训练仅使用划分后的合格训练样本；验证和测试记录不会作为训练目标。</p><Alert tone="info">任务在本机运行。完成后会生成候选版本，仍需评测并由你确认启用。</Alert>{personal && <p className="text-xs text-ink-3">基础模型：{effectiveBase}。本次最多使用{sampleBudget || "全部"}份合格训练样本，训练会占用本机计算资源。</p>}<ErrorBox error={error} /><div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setTrainingPreview(false)}>取消</Button><Button variant="primary" disabled={!!busy} onClick={() => void run("正在提交训练任务…", async () => { await api.post("/learning/jobs", jobBody); setTrainingPreview(false); setNotice("训练任务已提交，可在下方查看真实进度。"); refresh(); })}><Play size={15} />确认开始</Button></div></div></Modal>
    <Modal open={!!activation} onClose={() => { if (!busy) setActivation(null); }} title={activation?.rollback ? "确认回到这个版本" : "确认启用这个本机版本"}><div className="space-y-4"><p className="text-sm text-ink-2">将使用 {activation?.model.id.slice(0, 8)}，替换同一任务当前启用的版本。已有报告保留原有版本记录。</p>{activation?.model.personal_trial_available && !activation.model.gate?.passed && <Alert>这是基于个人开发资料的本机试用，自动检查通过不等于人工质量审核通过。建议先查看样例，可随时停用。启用模型不会自动发送消息。</Alert>}<ErrorBox error={error} /><div className="flex justify-end gap-2"><Button disabled={!!busy} onClick={() => setActivation(null)}>取消</Button><Button variant="primary" disabled={!!busy} onClick={() => void run("正在切换版本…", async () => { if (!activation) return; if (activation.rollback) await api.post("/learning/models/rollback", { model_id: activation.model.id, confirm: true, personal_trial: !!activation.model.personal_trial_available && !activation.model.gate?.passed }); else await api.post(`/learning/models/${encodeURIComponent(activation.model.id)}/activate`, { confirm: true, personal_trial: !!activation.model.personal_trial_available && !activation.model.gate?.passed }); setActivation(null); setNotice("模型版本已切换。"); refresh(); })}>确认切换</Button></div></div></Modal>
    <Modal open={!!comparison} onClose={() => setComparison(null)} title="版本对比" width={800}><div className="space-y-4">{comparison && !compatibleModelComparison(comparison) && <Alert tone="warn">所选版本的任务、资料或测试划分不同，下面仅并列展示，不能据此判定优劣。</Alert>}<div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b border-line"><th className="p-2">版本</th><th className="p-2">方案</th><th className="p-2">测试结果</th><th className="p-2">启用检查</th></tr></thead><tbody>{comparison?.map(model => <tr key={model.id} className="border-b border-line"><td className="p-2">{model.id.slice(0, 8)}</td><td className="p-2">{ARCHITECTURES[model.architecture] ?? model.architecture}</td><td className="p-2">{score(model)}</td><td className="p-2">{model.gate?.passed ? "通过" : "未通过"}</td></tr>)}</tbody></table></div><p className="text-xs text-ink-3">请比较相同资料版本、任务和测试划分。不同数据的成绩不能直接说明哪个模型更好。</p><details className="text-xs text-ink-3"><summary className="cursor-pointer">查看逐类指标及运行记录</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(comparison, null, 2)}</pre></details></div></Modal>
  </div>;
}

export function WorkbenchSchedule() {
  const { view, datasets, error: loadError, loading, refresh } = useLearningSnapshot(true);
  const [relationships] = useRelationships();
  const [draft, setDraft] = useState<LearningSchedule | null>(null);
  const [saved, setSaved] = useState(false);
  const [prepared, setPrepared] = useState(false);
  const { busy, error, run } = useWorkbenchAction();
  const current = draft ?? view?.schedules;
  const sourceMode = current?.source_mode ?? "dataset";
  const selectedRelationships = current?.relationship_ids ?? [];
  const readyBases = view?.capabilities.local_base_models?.filter(item => item.ready) ?? [];
  const validation = current ? scheduleValidation(current, {
    datasetIds: datasets.map(item => item.id), readyBaseIds: readyBases.map(item => item.id),
    loraReady: !!view?.capabilities.lora_dependencies, relationshipIds: relationships?.map(item => item.id) ?? [],
  }) : null;
  const canPrepare = selectedRelationships.length > 0 && selectedRelationships.every(id => relationships?.some(item => item.id === id));
  function update(patch: Partial<LearningSchedule>) { if (current) { setDraft({ ...current, ...patch }); setSaved(false); } }
  const statusLabels: Record<string, string> = { disabled: "已关闭", idle: "等待下次检查", preparing: "正在整理资料", ready: "资料已就绪", training: "正在训练候选版本", evaluating: "正在评测", failed: "需要处理" };
  return <div className="space-y-5"><ErrorBox error={error ?? loadError} />{loading ? <Spinner label="读取自动更新设置…" /> : current ? <>
    <Card title="让新记录成为下一次改进" hint="自动更新默认关闭；整理、训练和评测后，仍由你确认是否启用新版本。" right={<Timer size={22} className="text-brand" />}>
      <form className="space-y-4" onSubmit={event => {
        event.preventDefault(); if (validation) return;
        void run("正在保存更新设置…", async () => {
          await api.put("/learning/schedules", {
            enabled: current.enabled, dataset_id: current.dataset_id, weekly_day: current.weekly_day,
            min_new_samples: current.min_new_samples, pool: current.pool, architecture: current.architecture,
            base_model_id: current.base_model_id ?? null, source_mode: sourceMode,
            relationship_ids: selectedRelationships, timezone: current.timezone ?? "Asia/Shanghai",
          });
          setDraft(null); setSaved(true); refresh();
        });
      }}>
        <label className="flex items-center gap-3 rounded-2xl border border-line bg-paper/70 p-4"><input type="checkbox" checked={current.enabled} disabled={!!busy} onChange={event => update({ enabled: event.target.checked })} /><span><span className="block text-sm font-medium">开启自动检查与候选训练</span><span className="mt-1 block text-xs text-ink-3">服务运行期间检查本机资料，不自动上传或启用模型。</span></span></label>
        <Field label="从哪里学习新内容"><select className="input" value={sourceMode} disabled={!!busy} onChange={event => update(event.target.value === "archives" ? { source_mode: "archives", pool: "personal_reply", architecture: "reply_lora", dataset_id: null } : { source_mode: "dataset", dataset_id: null })}><option value="dataset">已有脱敏标注资料</option><option value="archives">选定好友的长期聊天档案</option></select></Field>
        {sourceMode === "archives" ? <fieldset className="rounded-2xl border border-line bg-paper/70 p-4" disabled={!!busy}>
          <legend className="px-1 text-sm font-medium">明确选择哪些好友的档案</legend>
          {!relationships ? <Spinner label="读取好友档案…" /> : relationships.length === 0 ? <p className="text-sm text-ink-3">尚无好友档案，请先创建对象并保存长期聊天记录。</p> : <div className="grid gap-3 sm:grid-cols-2">{relationships.map(item => <label key={item.id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={selectedRelationships.includes(item.id)} onChange={event => update({ relationship_ids: event.target.checked ? [...selectedRelationships, item.id] : selectedRelationships.filter(id => id !== item.id) })} />{item.name}</label>)}</div>}
          <p className="mt-3 text-xs leading-relaxed text-ink-3">只学习所选好友对话中你实际发出的文字。未校对内容、未知媒体和未采纳的 AI 草稿不直接当作答案；这不会自动打开微信采集。</p>
          <div className="mt-3 flex flex-wrap items-center gap-2"><Button type="button" disabled={!!busy || !canPrepare} onClick={() => void run("提交档案整理任务…", async () => {
            await api.post("/learning/jobs", { kind: "archive_prepare", source_mode: "archives", relationship_ids: selectedRelationships, dataset_id: current.dataset_id, pool: "personal_reply", architecture: "reply_lora" });
            setPrepared(true); refresh();
          })}>先整理一次，不训练</Button>{prepared && <Badge tone="mint">已提交，可到训练页查看进度</Badge>}</div>
        </fieldset> : <Field label="使用哪份资料"><select className="input" value={current.dataset_id ?? ""} disabled={!!busy} onChange={event => update({ dataset_id: event.target.value || null })}><option value="">请选择已整理的资料</option>{current.dataset_id && !datasets.some(item => item.id === current.dataset_id) && <option value={current.dataset_id}>原资料不可用，请重新选择</option>}{datasets.map(item => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {item.id.slice(0, 8)}</option>)}</select></Field>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="每周检查训练条件的日期"><select className="input" value={current.weekly_day} disabled={!!busy} onChange={event => update({ weekly_day: Number(event.target.value) })}>{["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map((day, i) => <option value={i} key={day}>{day}</option>)}</select></Field>
          <Field label="日期按哪个时区计算"><select className="input" value={current.timezone ?? "Asia/Shanghai"} disabled={!!busy} onChange={event => update({ timezone: event.target.value })}>{Array.from(new Set([current.timezone ?? "Asia/Shanghai", Intl.DateTimeFormat().resolvedOptions().timeZone, "Asia/Shanghai", "UTC"])).map(zone => <option value={zone} key={zone}>{zone === "Asia/Shanghai" ? "北京时间（UTC+8）" : zone === "UTC" ? "协调世界时（UTC）" : zone}</option>)}</select></Field>
          <Field label="至少积累多少新增合格样本" hint="默认 200 条；按新的训练样本计算，避免重复学习同一批。"><input className="input" type="number" min={200} max={100000} value={current.min_new_samples} disabled={!!busy} onChange={event => update({ min_new_samples: Number(event.target.value) })} /></Field>
          <Field label="更新什么能力"><select className="input" value={current.pool} disabled={!!busy || sourceMode === "archives"} onChange={event => update({ pool: event.target.value as DatasetPool, architecture: event.target.value === "personal_reply" ? "reply_lora" : "shared" })}><option value="offline_review">历史复盘判断</option><option value="realtime_review">实时判断（已人工复核）</option><option value="personal_reply">本人的表达</option></select></Field>
        </div>
        {current.pool === "personal_reply" && <Field label="个人模型使用的基础模型"><select className="input" value={current.base_model_id ?? ""} disabled={!!busy} onChange={event => update({ base_model_id: event.target.value || null })}><option value="">请选择已安装模型</option>{readyBases.map(model => <option value={model.id} key={model.id}>{model.id}</option>)}</select></Field>}
        <p className="text-xs leading-relaxed text-ink-3">每天检查所选本机来源，整理新快照；达到新增样本门槛、满足每周日期条件且没有任务占用时，创建候选训练。模型启用仍需单独确认。</p>
        {validation && <Alert tone="warn">{validation}</Alert>}
        <div className="flex flex-wrap items-center gap-3"><Button type="submit" variant="primary" disabled={!!busy || !!validation}>保存设置</Button><Button type="button" disabled={!!busy} onClick={refresh}>刷新运行情况</Button>{busy && <Spinner label={busy} />}{saved && <Badge tone="mint">已保存</Badge>}</div>
      </form>
    </Card>
    <Card title="最近运行情况" right={<Badge tone={view?.schedules.last_status === "failed" ? "warn" : view?.schedules.enabled ? "brand" : "neutral"}>{statusLabels[view?.schedules.last_status ?? ""] ?? (view?.schedules.enabled ? "已开启" : "已关闭")}</Badge>}>
      {view?.schedules.last_error && <Alert tone="warn">{view.schedules.last_error}</Alert>}
      <div className="mt-3 grid gap-3 text-sm text-ink-2 sm:grid-cols-2"><p>最近检查：{formatDate(view?.schedules.last_inspected_at)}</p><p>最近提交训练：{formatDate(view?.schedules.last_trained_at)}</p><p>最近检查发现的新增合格样本：{view?.schedules.last_new_samples ?? "未记录"}</p><p>计划时区：{view?.schedules.timezone ?? "Asia/Shanghai"}</p></div>
      {view?.schedules.prepare_job_id && <p className="mt-2 text-xs text-ink-3">整理任务 {view.schedules.prepare_job_id.slice(0, 8)}</p>}
      {view?.schedules.training_job_id && <p className="mt-2 text-xs text-ink-3">训练任务 {view.schedules.training_job_id.slice(0, 8)}</p>}
      <p className="mt-3 text-xs text-ink-3">服务关闭或电脑休眠时不会执行。开启后本页定期刷新，完整任务进度和失败原因见「训练与版本」。</p>
    </Card>
  </> : <Button onClick={refresh}>重新读取</Button>}</div>;
}
