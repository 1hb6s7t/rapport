import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { PriorLabelKey, PriorPolicyView } from "@/api/types";
import { Badge, Card, ErrorBox, Field } from "@/components/ui";

const LABELS: Record<PriorLabelKey, string> = { mbti: "MBTI", zodiac: "星座", chinese_zodiac: "属相" };
const KEYS: PriorLabelKey[] = ["mbti", "zodiac", "chinese_zodiac"];

/** 设置 → 个性化权重：辅助标签（MBTI / 星座 / 属相）的总预算、三者分配与衰减速度。
 *  只影响画像假设与回复排序，不进入客观关系分数；默认 15%、4 : 2 : 1，可随时恢复。 */
export function PriorPolicyCard({ onSaved }: { onSaved?: () => void } = {}) {
  const [view, setView] = useState<PriorPolicyView | null>(null);
  const [alpha0, setAlpha0] = useState(15); // percent
  const [ratio, setRatio] = useState<Record<PriorLabelKey, number>>({ mbti: 4, zodiac: 2, chinese_zodiac: 1 });
  const [scale, setScale] = useState(40);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  function adopt(v: PriorPolicyView) {
    setView(v);
    setAlpha0(Math.round(v.alpha0 * 100));
    setRatio({ mbti: v.ratio.mbti, zodiac: v.ratio.zodiac, chinese_zodiac: v.ratio.chinese_zodiac });
    setScale(v.scale);
  }
  useEffect(() => { api.get<PriorPolicyView>("/preferences/prior").then(adopt).catch(setErr); }, []);

  const total = KEYS.reduce((a, k) => a + (ratio[k] || 0), 0);
  const share = (k: PriorLabelKey) => (total > 0 ? (alpha0 * (ratio[k] || 0)) / total : 0);
  const dirty = view !== null && (Math.round(view.alpha0 * 100) !== alpha0 || KEYS.some((k) => view.ratio[k] !== ratio[k]) || view.scale !== scale);

  async function save(reset: boolean) {
    setBusy(true);
    setErr(null);
    setSaved(false);
    try {
      adopt(await api.patch<PriorPolicyView>("/preferences/prior", reset ? { reset: true } : { alpha0: alpha0 / 100, scale, ...ratio }));
      setSaved(true);
      onSaved?.();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="个性化权重（MBTI / 星座 / 属相）">
      {!view ? <p className="text-sm text-ink-3">读取中…</p> : (
        <div className="space-y-3 text-sm">
          <p className="text-xs text-ink-3">
            聊天证据始终是客观关系分数的唯一依据。这里只决定辅助标签在画像假设和回复排序里最多占多少、三者之间怎么分，以及随证据累积衰减多快。默认 15%、4 : 2 : 1；设为 0 表示完全不用标签。
          </p>
          <Field label={`标签总预算 ${alpha0}%`} hint={`其余 ${100 - alpha0}% 归聊天证据与「未知」；有效证据越多，标签权重还会继续衰减。`}>
            <input type="range" min={0} max={100} step={1} value={alpha0} onChange={(e) => setAlpha0(Number(e.target.value))} className="w-full" aria-label="标签总预算" />
          </Field>
          <div className="grid grid-cols-3 gap-2">
            {KEYS.map((k) => (
              <Field key={k} label={LABELS[k]} hint={`≈ ${share(k).toFixed(1)}%`}>
                <input className="input" type="number" min={0} max={100} step={1} value={ratio[k]} onChange={(e) => setRatio({ ...ratio, [k]: Math.max(0, Math.min(100, Number(e.target.value) || 0)) })} aria-label={`${LABELS[k]} 份数`} />
              </Field>
            ))}
          </div>
          <details>
            <summary className="cursor-pointer text-xs text-ink-3">高级：衰减速度</summary>
            <div className="mt-2">
              <Field label={`每累计 ${scale} 条有效证据，标签权重缩到原来的约 37%`} hint="数字越大，标签的影响保留得越久。默认 40。">
                <input className="input w-28" type="number" min={5} max={500} step={5} value={scale} onChange={(e) => setScale(Math.max(5, Math.min(500, Number(e.target.value) || 40)))} aria-label="衰减速度" />
              </Field>
            </div>
          </details>
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn btn-primary" disabled={busy || !dirty} onClick={() => save(false)}>保存</button>
            <button className="btn btn-ghost" disabled={busy || (view.is_default && !dirty)} onClick={() => save(true)}>恢复默认</button>
            <Badge tone={view.is_default ? "lavender" : "accent"}>{view.is_default ? "当前：默认" : "当前：自定义"}</Badge>
            {saved && <span className="text-xs text-ink-3">已保存，下次分析与草稿生效；已有报告不改写。</span>}
          </div>
          <ErrorBox error={err} />
        </div>
      )}
    </Card>
  );
}
