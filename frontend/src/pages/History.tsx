import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { DriveExplain, OutcomesView, Relationship, RelationshipOverview } from "@/api/types";
import { RelationshipPicker, useRelationships } from "@/components/RelationshipPicker";
import { Alert, Badge, Card, Empty, ErrorBox, Field, Spinner } from "@/components/ui";
import { CHINESE_ZODIAC, DIRECTION_LABELS, MBTI_TYPES, STAGE_LABELS, TENDENCY_LABELS, ZODIAC } from "@/i18n/labels";
import { DRIVE_VAR_LABELS } from "@/i18n/labels";
import { useFlow } from "@/store";

const KIND_LABELS: Record<string, string> = { intent: "逐句语义", observation: "观察", action: "行动", landmine: "雷区", stage: "阶段", trait: "特质", affinity: "好感度", boundary: "边界" };

/** The check-in's five answers. Plain words: the user is reporting what they saw, not a category. */
const OUTCOME_LABELS: Record<string, string> = {
  better: "变好了",
  same: "和以前一样",
  worse: "变差了",
  ended: "已经不联系了",
  unknown: "说不清",
};

export function HistoryPage() {
  const [flow, setFlow] = useFlow();
  const [rels, refresh] = useRelationships();
  const [ov, setOv] = useState<RelationshipOverview | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const rid = flow.relationshipId;

  useEffect(() => {
    setOv(null);
    api.get<RelationshipOverview>(`/relationships/${rid}/overview`).then(setOv).catch(setErr);
  }, [rid]);

  if (err) return <ErrorBox error={err} />;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">历史与变化</h1>
        <p className="text-sm text-ink-2 mt-1">
          这里只显示<strong>真实产生过的记录</strong>：每次确认导入并完成分析才会产生一个点。
          相同材料与相同规则版本不会重复出点；规则或模型变更会标记为不可比，而不是悄悄连成一条曲线。
        </p>
      </div>

      <Card title="对象">
        <RelationshipPicker label="查看哪个对象的历史" hint="每个对象有自己的证据账本、曲线、阶段与画像版本。" />
        {ov && <RelationshipEditor rel={ov.relationship} onSaved={async () => { await refresh(); setOv(await api.get<RelationshipOverview>(`/relationships/${rid}/overview`)); }} onDeleted={async () => { setFlow({ relationshipId: "default" }); await refresh(); }} />}
      </Card>

      {!ov ? <Spinner label="读取历史…" /> : <Overview ov={ov} />}
      <CheckIn rid={rid} />
      <DriveStrip rid={rid} />
      {rels && rels.length > 1 && (
        <p className="text-xs text-ink-3">共 {rels.length} 个对象。切换上方选择器查看其它对象；删除对象会一并删除其全部材料、报告与历史，不影响其它对象。</p>
      )}
    </div>
  );
}

/** The two-week check-in. It asks once, in the place the user is already looking at the past, and
 *  says plainly that the answer is a record rather than a score input. */
function DriveStrip({ rid }: { rid: string }) {
  const [ex, setEx] = useState<DriveExplain | null>(null);
  useEffect(() => {
    setEx(null);
    api.get<DriveExplain>(`/relationships/${rid}/drive`).then(setEx).catch(() => setEx(null));
  }, [rid]);
  if (!ex || !ex.enabled) return null;
  const bar = (k: "drive" | "inhibition" | "novelty") => (
    <div key={k} className="flex items-center gap-2 text-xs">
      <span className="w-8 text-ink-3">{DRIVE_VAR_LABELS[k]}</span>
      <div className="flex-1 h-2 rounded bg-paper overflow-hidden"><div className="h-2 bg-accent" style={{ width: `${Math.round(ex[k] * 100)}%` }} /></div>
      <span className="w-10 text-right mono">{ex[k].toFixed(2)}</span>
    </div>
  );
  return (
    <Card title="主动提醒状态">
      <div className="space-y-1">{(["drive", "inhibition", "novelty"] as const).map(bar)}</div>
      <p className="text-xs text-ink-3 mt-2">
        上小时触发概率 {ex.p_last_hour}{ex.why_not ? `；${ex.why_not}` : ""}。今天 {ex.today_count}/{ex.daily_cap} 条。
        <Link to="/inbox" className="ml-1">收件箱</Link>
      </p>
    </Card>
  );
}

function CheckIn({ rid }: { rid: string }) {
  const [view, setView] = useState<OutcomesView | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setView(null);
    setErr(null);
    api.get<OutcomesView>(`/relationships/${rid}/outcomes`).then(setView).catch(setErr);
  }, [rid]);

  const p = view?.prompt ?? null;
  if (err || !p) return null; // nothing to ask: stay out of the way

  // A const arrow keeps the null-narrowing above inside the closure; a hoisted `function` loses it.
  const answer = async (outcome: string) => {
    setBusy(true);
    try {
      await api.post(`/relationships/${rid}/outcomes`, { report_id: p.report_id, outcome });
      setView((v) => (v ? { ...v, prompt: null } : v));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="后来怎么样了">
      <p className="text-sm">{p.question}</p>
      <p className="text-xs text-ink-3 mt-1">
        上次分析在 {p.days_since} 天前。这个回答只存在本机，用于以后校准分数方向，永远不进入好感度分数。
      </p>
      <div className="flex flex-wrap gap-2 mt-3">
        {p.options.map((o) => (
          <button
            key={o}
            type="button"
            disabled={busy}
            onClick={() => answer(o)}
            className="px-3 py-1.5 rounded-md border border-line text-sm hover:bg-surface-2 disabled:opacity-50"
          >
            {OUTCOME_LABELS[o] ?? o}
          </button>
        ))}
      </div>
    </Card>
  );
}

function Overview({ ov }: { ov: RelationshipOverview }) {
  const trend = ov.trend;
  const stages = ov.stage_history.points;
  const personas = ov.persona_history.versions;
  const comparable = trend.points.filter((p) => p.comparable && p.total !== null);
  const fb = ov.feedback;
  return (
    <>
      <Card title="概览">
        <dl className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
          <Stat k="报告" v={String(ov.relationship.counts.reports ?? 0)} />
          <Stat k="可比历史点" v={String(comparable.length)} />
          <Stat k="有效证据量" v={ov.n_eff.toFixed(1)} hint="只统计已确认导入、去重后的聊天事件；AI 输出与参考例不计入" />
          <Stat k="模型调用" v={`${ov.llm.calls} 次${ov.llm.cost_cny ? ` · ≈¥${ov.llm.cost_cny}` : ""}`} hint={ov.llm.truncated ? `${ov.llm.truncated} 次被截断` : undefined} />
          <Stat k="你的反馈" v={`${fb.total} 条`} hint={fb.total ? Object.entries(fb.by_kind).map(([k, v]) => `${KIND_LABELS[k] ?? k} 👍${v.agree ?? 0} 👎${v.disagree ?? 0}`).join("；") : undefined} />
        </dl>
      </Card>

      <Card title="好感度的真实快照">
        {comparable.length === 0 ? <Empty>还没有可比的历史点（对同一对象做两次不同材料的分析就会出现）</Empty> : (
          <table className="w-full text-sm">
            <thead className="text-xs text-ink-3 text-left">
              <tr><th className="py-1">时间</th><th>分数</th><th>覆盖度</th><th>有效证据量</th><th>报告</th></tr>
            </thead>
            <tbody>
              {comparable.map((p) => (
                <tr key={p.id} className="border-t border-line">
                  <td className="py-1">{new Date(p.created_at).toLocaleString()}</td>
                  <td>{p.total ?? "—"}</td>
                  <td>{p.coverage.toFixed(2)}</td>
                  <td>{p.n_eff.toFixed(1)}</td>
                  <td><Link className="mono text-xs" to={`/report/${p.report_id}`}>{p.report_id.slice(0, 8)}</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="text-xs text-ink-3 mt-2">{trend.note}</p>
        {trend.points.length > comparable.length && (
          <p className="text-xs text-ink-3 mt-1">另有 {trend.points.length - comparable.length} 个点因为版本变更或未出分而不参与连线。</p>
        )}
      </Card>

      <Card title="关系阶段的变化">
        {stages.length === 0 ? <Empty>还没有报告</Empty> : (
          <ul className="text-sm space-y-2">
            {stages.map((p) => (
              <li key={p.report_id} className="border-t border-line pt-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span>{new Date(p.created_at).toLocaleString()}</span>
                  <Badge tone="info">{STAGE_LABELS[p.value ?? ""] ?? p.value ?? "—"}</Badge>
                  {p.change && <Badge tone={p.change.direction === "regress" ? "warn" : "neutral"}>{DIRECTION_LABELS[p.change.direction] ?? p.change.direction}</Badge>}
                  <Link className="mono text-xs" to={`/report/${p.report_id}`}>{p.report_id.slice(0, 8)}</Link>
                </div>
                {p.change && (
                  <p className="text-xs text-ink-3 mt-1">
                    {STAGE_LABELS[p.change.before] ?? p.change.before} → {STAGE_LABELS[p.change.after] ?? p.change.after}：{p.change.reason}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="对方画像的版本">
        {personas.length === 0 ? <Empty>还没有 AI 分析产生过画像（规则模式不生成画像）</Empty> : (
          <ul className="text-sm space-y-3">
            {personas.map((v) => (
              <li key={`${v.report_id}-${v.version}`} className="border-t border-line pt-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={v.active ? "good" : "neutral"}>v{v.version}</Badge>
                  {v.active ? <span className="text-xs text-ink-3">当前生效</span> : <span className="text-xs text-ink-3">已被后续版本取代</span>}
                  <span className="text-xs text-ink-3">{new Date(v.created_at).toLocaleString()}</span>
                  <Link className="mono text-xs" to={`/report/${v.report_id}`}>{v.report_id.slice(0, 8)}</Link>
                </div>
                {v.change_notes.length > 0 ? (
                  <ul className="text-xs text-ink-2 mt-1 space-y-0.5">
                    {v.change_notes.map((c, i) => (
                      <li key={`${c.field}-${i}`}>
                        {c.kind === "added" ? "新增" : c.kind === "removed" ? "移除" : "修正"}：{c.field} {c.before && c.after ? `${c.before} → ${c.after}` : c.after || c.before}（{c.reason}）
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-ink-3 mt-1">{v.version === 1 ? "首个版本，没有可比对的变化" : "与上一版一致"}</p>
                )}
                {Object.keys(v.tendencies).length > 0 && (
                  <p className="text-xs text-ink-3 mt-1">倾向：{Object.entries(v.tendencies).map(([k, val]) => `${TENDENCY_LABELS[k] ?? k}=${val}`).join("，")}</p>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2"><Alert tone="info">旧版本保留内容但不再生效；删除对应报告会让该版本失效。</Alert></div>
      </Card>
    </>
  );
}

function Stat({ k, v, hint }: { k: string; v: string; hint?: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-3">{k}</dt>
      <dd className="font-medium break-words">{v}</dd>
      {hint && <dd className="text-xs text-ink-3">{hint}</dd>}
    </div>
  );
}

function RelationshipEditor({ rel, onSaved, onDeleted }: { rel: Relationship; onSaved: () => Promise<void>; onDeleted: () => Promise<void> }) {
  const [name, setName] = useState(rel.name);
  const [note, setNote] = useState(rel.note);
  const [labels, setLabels] = useState(rel.labels);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => { setName(rel.name); setNote(rel.note); setLabels(rel.labels); }, [rel.id, rel.name, rel.note, rel.labels]);
  const dirty = name !== rel.name || note !== rel.note || JSON.stringify(labels) !== JSON.stringify(rel.labels);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await api.patch(`/relationships/${rel.id}`, { name, note, labels });
      await onSaved();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!confirm(`删除对象「${rel.name}」及其全部材料、报告、历史与反馈？此操作不可撤销，不影响其它对象。`)) return;
    setBusy(true);
    try {
      await api.del(`/relationships/${rel.id}`);
      await onDeleted();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="mt-3">
      <summary className="text-sm cursor-pointer text-ink-2">对象设置（名称、备注、长期标签）</summary>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 mt-2">
        <Field label="名称"><input className="input" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="备注"><input className="input" value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} /></Field>
        <Field label="对方 MBTI">
          <select className="input" value={labels.mbti ?? ""} onChange={(e) => setLabels({ ...labels, mbti: e.target.value || null })}>
            <option value="">不填</option>{MBTI_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="对方星座">
          <select className="input" value={labels.zodiac ?? ""} onChange={(e) => setLabels({ ...labels, zodiac: e.target.value || null })}>
            <option value="">不填</option>{ZODIAC.map(([v, l]) => <option key={v} value={v}>{l}座</option>)}
          </select>
        </Field>
        <Field label="对方属相">
          <select className="input" value={labels.chinese_zodiac ?? ""} onChange={(e) => setLabels({ ...labels, chinese_zodiac: e.target.value || null })}>
            <option value="">不填</option>{CHINESE_ZODIAC.map(([v, l]) => <option key={v} value={v}>属{l}</option>)}
          </select>
        </Field>
      </div>
      <p className="text-xs text-ink-3 mt-1">标签只作画像的起始假设，不进入分数；这里保存的标签会在下次分析的「标签与目标」页自动填入。</p>
      <div className="flex gap-2 mt-2">
        <button className="btn btn-primary py-1" disabled={busy || !dirty || !name.trim()} onClick={save}>保存</button>
        {!rel.is_default && <button className="btn btn-danger py-1" disabled={busy} onClick={remove}>删除该对象</button>}
      </div>
      <ErrorBox error={err} />
    </details>
  );
}
