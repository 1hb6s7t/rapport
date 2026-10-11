import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { EmotionConfigView, OfflineComponentStatus } from "@/api/types";
import { Alert, Badge, Card, Field } from "@/components/ui";

/** 设置 → 分析与个性化: the offline analysis layer switch for rules mode. */
export function EmotionModelCard({ onSaved }: { onSaved?: () => void }) {
  const [v, setV] = useState<EmotionConfigView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [maxMsgs, setMaxMsgs] = useState("120");

  useEffect(() => {
    api.get<EmotionConfigView>("/preferences/emotion")
      .then((value) => { setV(value); setMaxMsgs(String(value.config.max_messages)); })
      .catch((e) => setErr(String(e)));
  }, []);

  async function patch(body: Partial<EmotionConfigView["config"]>) {
    try {
      const value = await api.patch<EmotionConfigView>("/preferences/emotion", body);
      setV(value);
      setMaxMsgs(String(value.config.max_messages));
      onSaved?.();
    } catch (e) {
      setErr(String(e));
    }
  }

  if (!v) {
    return (
      <Card title="离线分析层（规则模式）">
        {err ? <Alert tone="warn">{err}</Alert> : <p className="text-sm text-ink-3">读取中…</p>}
      </Card>
    );
  }
  const { config, model } = v;
  const emo = model.emotion_model;
  const bge = model.bge;
  const anchors = model.anchors;
  const cal = model.calibration;
  const markers = model.markers;
  const groundedReady = Boolean(bge?.available && anchors?.available && emo.available);
  const chip = (c: OfflineComponentStatus | undefined, label: string) => (
    <Badge tone={c?.available ? "good" : "warn"}>{label}{c?.available ? " 就绪" : " 缺失"}</Badge>
  );
  return (
    <Card title="离线分析层（规则模式）">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-ink-3">
          规则模式默认只用词表判断。开启后，对方最近的消息会由本机的研究层逐句判断：
          BGE-small-zh 向量在本机语料里检索最相似的例句 + 温度校准后的情绪小模型 + 中文礼貌标记，
          完全离线、不联网、不产生费用、不进入客观关系分数。AI 分析模式下不使用它。
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {chip(emo, "情绪小模型")}
          {chip(bge, "BGE 检索编码器")}
          {chip(anchors, "近邻语料库")}
          {chip(cal, "温度校准")}
          {chip(markers, "中文礼貌标记")}
        </div>
        <p className="text-xs text-ink-3">
          {anchors?.count ? `近邻语料库：${anchors.count} 条本机例句 · ${anchors.quality ?? ""}` : ""}
          {cal?.available && cal.temperature ? ` · 温度 ${cal.temperature}` : ""}
          {bge?.size_bytes ? ` · BGE ${(bge.size_bytes / 1048576).toFixed(0)} MB` : ""}
        </p>

        <fieldset className="space-y-2">
          <legend className="text-xs text-ink-3">判断方式</legend>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name="engine"
              checked={config.engine === "grounded"}
              disabled={!groundedReady}
              onChange={() => void patch({ engine: "grounded" })}
            />
            <span>
              研究层（推荐）：BGE 近邻检索 + 校准情绪小模型 + 礼貌标记
              <span className="block text-xs text-ink-3">
                对链接/代码/通知会直接判为「无明显情绪」；标签可回溯到最相似的例句标签；把握不足时显示「材料不足」。
                {!groundedReady && "（需要 BGE 编码器、近邻语料库与情绪小模型都就绪）"}
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="radio"
              name="engine"
              checked={config.engine === "structbert"}
              disabled={!emo.available}
              onChange={() => void patch({ engine: "structbert" })}
            />
            <span>
              只用情绪小模型：StructBERT-tiny 单句判断
              <span className="block text-xs text-ink-3">
                本项目实测：它对非情绪内容也会硬贴标签（链接判成「不满」、「想你了」判成「伤心」），只作参考。
              </span>
            </span>
          </label>
        </fieldset>

        {!emo.available && (
          <Alert tone="warn">
            本机还没有可用的情绪模型（{emo.reason}）。安装步骤见 EMOTION-MODEL.md：下载 → 导出 ONNX → 校验，全部在本机完成。
          </Alert>
        )}
        {!anchors?.available && (
          <Alert tone="warn">
            近邻语料库尚未构建（{anchors?.reason}）：运行 <code>tools/build_anchors.py</code> 用本机数据集生成，
            文件加密保存在数据目录，不会写入明文。
          </Alert>
        )}

        <label className="flex items-center gap-2">
          <input type="checkbox" checked={config.enabled} disabled={!emo.available} onChange={(e) => void patch({ enabled: e.target.checked })} />
          在规则模式里启用离线分析层
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={config.use_markers} onChange={(e) => void patch({ use_markers: e.target.checked })} />
          用中文礼貌标记补充语用线索（jieba + ConvoKit-zh）
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={config.use_changepoints} onChange={(e) => void patch({ use_changepoints: e.target.checked })} />
          在趋势里显示探索性变化点（ruptures PELT，会话数足够时才显示）
        </label>

        <Field label="每次分析最多判断多少条消息" hint="10–500 条，默认 120。只判断对方发来的文字消息，取最近的那些；条数越多，分析耗时越长。">
          <input
            className="input w-24"
            type="number"
            min={10}
            max={500}
            value={maxMsgs}
            onChange={(e) => setMaxMsgs(e.target.value)}
            onBlur={() => {
              const n = Number(maxMsgs);
              if (Number.isFinite(n) && n !== config.max_messages) void patch({ max_messages: n });
              else setMaxMsgs(String(config.max_messages));
            }}
          />
        </Field>
        <p className="text-xs text-ink-3">
          判断结果出现在报告的「语义与情绪（逐句）」里，每条标注来源与各信号投票；把握低的句子显示为「材料不足」，不会硬下结论。
          离线评测（与弱标注的一致度、覆盖率、ECE）见 output/grounded-eval.md。
        </p>
        {err && <Alert tone="warn">{err}</Alert>}
      </div>
    </Card>
  );
}
