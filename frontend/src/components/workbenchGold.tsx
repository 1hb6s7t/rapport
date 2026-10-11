import { useCallback, useEffect, useState } from "react";
import { ClipboardCheck } from "lucide-react";
import { api } from "@/api/client";
import type { DatasetManifest } from "@/components/workbenchDataset";
import { Alert, Badge, Button, Card, EmptyState, ErrorBox, Field, StatTile } from "@/components/ui";
import { useWorkbenchAction } from "@/lib/workbench";

interface GoldProgress {
  has_plan: boolean;
  plan: { dataset_id: string; pool: string; size: number; seed: number; quotas: Record<string, number>; created_at: string } | null;
  total: number;
  labelled: number;
  skipped: number;
  remaining: number;
  per_emotion: Record<string, number>;
  agreement_with_ai: number | null;
  agreement_per_ai_label: Record<string, number>;
  stored_labels_total: number;
}
interface GoldSample {
  sample_id: string;
  position: number;
  text: string;
  context: { sender: "me" | "other"; text: string; ts?: string | null }[];
  split: string;
  emotions: string[];
  intents: string[];
}
interface GoldNext { done: boolean; sample: GoldSample | null; progress: GoldProgress }

const EMOTION_HINTS: Record<string, string> = {
  "不满": "生气、嫌弃、责备",
  "伤心": "难过、失落、委屈",
  "开心": "高兴、兴奋、满足",
  "担忧": "担心、不安、害怕",
  "亲近": "亲昵、喜欢、想念",
  "混合": "几种情绪同时明显",
  "无明显情绪": "平铺直叙、链接、通知、功能性对话",
  "无奈": "无力、认命、算了",
  "惊讶困惑": "意外、不解、反问",
  "不确定": "看不出来，材料不够",
};
const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"];

/** 工作台 → 人工金标: label a stratified sample of your own messages by hand, without seeing the AI label. */
export function WorkbenchGold() {
  const { busy, error, run } = useWorkbenchAction();
  const [datasets, setDatasets] = useState<DatasetManifest[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [progress, setProgress] = useState<GoldProgress | null>(null);
  const [next, setNext] = useState<GoldNext | null>(null);
  const [size, setSize] = useState("400");
  const [emotion, setEmotion] = useState<string | null>(null);
  const [intent, setIntent] = useState<string>("");
  const [note, setNote] = useState("");

  useEffect(() => {
    api.get<{ items: DatasetManifest[] }>("/datasets").then((data) => {
      const built = data.items.filter((d) => d.status === "built");
      setDatasets(built);
      if (built.length && !selected) setSelected(built[0].id);
    }).catch(() => setDatasets([]));
  }, [selected]);

  const refresh = useCallback(async (id: string) => {
    const p = await api.get<GoldProgress>(`/gold/plan?dataset_id=${encodeURIComponent(id)}`);
    setProgress(p);
    if (p.has_plan) {
      setNext(await api.get<GoldNext>(`/gold/next?dataset_id=${encodeURIComponent(id)}`));
    } else {
      setNext(null);
    }
  }, []);

  useEffect(() => {
    if (!selected) return;
    void run("读取金标进度…", async () => { await refresh(selected); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  function receive(value: GoldNext) {
    setNext(value);
    setProgress(value.progress);
    setEmotion(null);
    setIntent("");
    setNote("");
  }

  async function save(skipped = false) {
    const sample = next?.sample;
    if (!sample || (!skipped && !emotion)) return;
    await run(skipped ? "跳过这条…" : "保存这条…", async () => {
      receive(await api.post<GoldNext>("/gold/label", {
        dataset_id: selected, sample_id: sample.sample_id, emotion: skipped ? null : emotion,
        intent: intent || null, skipped, note,
      }));
    });
  }

  useEffect(() => {
    if (!next?.sample) return;
    const sample = next.sample;
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.tagName === "SELECT")) return;
      const index = KEYS.indexOf(event.key);
      if (index >= 0 && index < sample.emotions.length) { setEmotion(sample.emotions[index]); event.preventDefault(); }
      else if (event.key === "Enter" && emotion) { event.preventDefault(); void save(false); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [next, emotion, intent, note]);

  if (!datasets.length) {
    return <EmptyState title="还没有可用于金标的数据集。" hint="先在「数据与复核」里整理一个数据集版本，再回来抽样标注。" />;
  }
  const sample = next?.sample ?? null;
  return <div className="space-y-4">
    <Card title="人工金标" right={<ClipboardCheck size={18} className="text-brand-ink" />}>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-ink-3">
          离线研究层现在报的所有数字都是「与 AI 弱标注的一致度」。这里由你亲手给一批自己的消息贴情绪标签：
          抽样时少数类（不满、伤心、担忧）有保底份额，标注时不显示 AI 的标签，避免被带着走。标完后用
          <code className="mx-1">tools/evaluate_grounded.py --gold</code> 重跑评测，就能得到相对人工金标的成绩。
        </p>
        <Field label="数据集版本">
          <select className="input" value={selected} onChange={(e) => setSelected(e.target.value)} disabled={!!busy}>
            {datasets.map((d) => <option key={d.id} value={d.id}>{d.id.slice(0, 8)} · {new Date(d.created_at).toLocaleString("zh-CN")}</option>)}
          </select>
        </Field>
        {progress && !progress.has_plan && (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="抽样条数" hint="50–2000，默认 400；只从 validation/test 切分抽，和近邻库（train）不重叠。">
              <input className="input w-28" type="number" min={50} max={2000} value={size} onChange={(e) => setSize(e.target.value)} />
            </Field>
            <Button variant="primary" disabled={!!busy} onClick={() => void run("正在抽样…", async () => {
              const p = await api.post<GoldProgress>("/gold/plan", { dataset_id: selected, size: Number(size) || 400 });
              setProgress(p);
              await refresh(selected);
            })}>建立金标计划</Button>
          </div>
        )}
        {progress?.has_plan && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StatTile label="计划条数" value={progress.total} />
            <StatTile label="已标注" value={progress.labelled} sub={progress.skipped ? `跳过 ${progress.skipped}` : undefined} />
            <StatTile label="剩余" value={progress.remaining} />
            <StatTile label="与 AI 标签一致" value={progress.agreement_with_ai == null ? "—" : `${Math.round(progress.agreement_with_ai * 100)}%`} sub="标够 30 条再看" />
          </div>
        )}
        <ErrorBox error={error} />
      </div>
    </Card>

    {sample && (
      <Card title={`第 ${sample.position + 1} / ${progress?.total ?? "?"} 条`} right={<Badge>{{ validation: "验证", test: "测试" }[sample.split] ?? sample.split}</Badge>}>
        <div className="space-y-3 text-sm">
          <div className="space-y-1 rounded-xl bg-paper/70 p-3">
            {sample.context.length === 0 && <p className="text-xs text-ink-3">（这条前面没有可显示的上下文）</p>}
            {sample.context.map((m, i) => (
              <div key={i} className={`flex ${m.sender === "me" ? "justify-end" : "justify-start"}`}>
                <span className={`max-w-[80%] rounded-xl px-3 py-1.5 text-xs ${m.sender === "me" ? "bg-brand-mist text-ink" : "bg-card text-ink-2"}`}>
                  <span className="mr-1 text-ink-3">{m.sender === "me" ? "我" : "对方"}</span>{m.text}
                </span>
              </div>
            ))}
            <div className="flex justify-start pt-1">
              <span className="max-w-[80%] rounded-xl border-2 border-brand-ink bg-card px-3 py-2 font-medium">
                <span className="mr-1 text-xs text-ink-3">对方 · 这一句</span>{sample.text}
              </span>
            </div>
          </div>
          <p className="text-xs text-ink-3">对方发这句时的情绪是什么？按数字键 1–9、0 选择，回车保存。拿不准就选「不确定」，不要猜。</p>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {sample.emotions.map((label, index) => (
              <button
                key={label}
                type="button"
                onClick={() => setEmotion(label)}
                className={`rounded-xl border px-3 py-2 text-left transition-colors ${emotion === label ? "border-brand-ink bg-brand-mist font-medium" : "border-line bg-card hover:bg-paper"}`}
              >
                <span className="mr-1 text-xs text-ink-3">{KEYS[index]}</span>{label}
                <span className="block text-xs text-ink-3">{EMOTION_HINTS[label] ?? ""}</span>
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="语用（可不填）">
              <select className="input" value={intent} onChange={(e) => setIntent(e.target.value)}>
                <option value="">不标</option>
                {sample.intents.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </Field>
            <Field label="备注（可不填）">
              <input className="input w-64" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
            </Field>
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <Button disabled={!!busy || (progress?.stored_labels_total ?? 0) === 0} onClick={() => void run("撤销上一条…", async () => {
              receive(await api.post<GoldNext>("/gold/undo", { dataset_id: selected }));
            })}>撤销上一条</Button>
            <Button disabled={!!busy} onClick={() => void save(true)}>跳过（看不懂 / 非对方消息）</Button>
            <Button variant="primary" disabled={!!busy || !emotion} onClick={() => void save(false)}>保存并下一条</Button>
          </div>
        </div>
      </Card>
    )}

    {next?.done && progress && (
      <Alert tone="good" title="这一批标完了">
        共 {progress.labelled} 条有效标注、{progress.skipped} 条跳过。
        在终端运行 <code>tools/evaluate_grounded.py --gold</code> 可以得到各方法相对人工金标的 macro-F1；
        想再标一批，可以在计划里选择「替换」重新抽样（已标的不会丢）。
      </Alert>
    )}
    {progress?.has_plan && progress.labelled >= 30 && (
      <Card title="到目前为止">
        <div className="flex flex-wrap gap-2 text-xs">
          {Object.entries(progress.per_emotion).sort((a, b) => b[1] - a[1]).map(([k, v]) => <Badge key={k}>{k} {v}</Badge>)}
        </div>
        {Object.keys(progress.agreement_per_ai_label).length > 0 && (
          <p className="mt-2 text-xs text-ink-3">
            按 AI 标签分：{Object.entries(progress.agreement_per_ai_label).map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(" · ")}
            （数字低说明 AI 弱标注在这一类上不可靠）
          </p>
        )}
      </Card>
    )}
  </div>;
}
