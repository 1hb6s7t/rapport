import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { AnalysisView, BirthDate, Goal, Person, SettingsView } from "@/api/types";
import { Alert, Card, ErrorBox, Field } from "@/components/ui";
import { CHINESE_ZODIAC, GOAL_LABELS, MBTI_TYPES, ZODIAC } from "@/i18n/labels";
import { refreshRelationships, useRelationships } from "@/components/RelationshipPicker";
import { getFlowSnapshot, useFlow } from "@/store";

export function ProfilePage() {
  const nav = useNavigate();
  const [flow, setFlow] = useFlow();
  const p = flow.profile;
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [reload, setReload] = useState(0);
  const activeRelationship = useRef(flow.relationshipId);
  activeRelationship.current = flow.relationshipId;
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [rels] = useRelationships();
  const rel = rels?.find((r) => r.id === flow.relationshipId);
  useEffect(() => {
    api.get<SettingsView>("/settings").then(setSettings).catch(() => {});
  }, []);
  useEffect(() => {
    let alive = true;
    setLoaded(null);
    setSaved(false);
    setErr(null);
    Promise.all([
      api.get<Person>("/relationships/self/profile"),
      api.get<Person>(`/relationships/${flow.relationshipId}/profile`),
    ]).then(([me, other]) => {
      if (!alive) return;
      setFlow({ profile: { ...getFlowSnapshot().profile, me, other } });
      setLoaded(flow.relationshipId);
    }).catch((e) => { if (alive) setErr(e); });
    return () => { alive = false; };
    // Intent/goal draft remains local; durable people reload only when the selected friend changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.relationshipId, setFlow, reload]);

  function upd(path: "me" | "other", key: string, v: unknown) {
    setSaved(false);
    setFlow({ profile: { ...p, [path]: { ...p[path], source: "user", confirmed: false, [key]: v === "" ? null : v } } });
  }

  function birthdayPart(who: "me" | "other", key: "year" | "month" | "day", value: string) {
    const next: BirthDate = { calendar: "gregorian", precision: "unknown", ...p[who].birthday, [key]: value ? Number(value) : null };
    next.precision = next.year && next.month && next.day ? "day" : next.year && next.month ? "year_month" : next.year ? "year" : next.month && next.day ? "month_day" : "unknown";
    upd(who, "birthday", next);
  }

  async function saveProfiles() {
    const rid = flow.relationshipId;
    const results = await Promise.allSettled([
      api.put<Person>("/relationships/self/profile", p.me),
      api.put<Person>(`/relationships/${rid}/profile`, p.other),
    ]);
    if (activeRelationship.current !== rid) return null;
    const me = results[0].status === "fulfilled" ? results[0].value : p.me;
    const other = results[1].status === "fulfilled" ? results[1].value : p.other;
    const profile = { ...p, me, other };
    setFlow({ profile });
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
    setSaved(true);
    await refreshRelationships();
    return activeRelationship.current === rid ? profile : null;
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try { await saveProfiles(); } catch (e) { setErr(e); } finally { setBusy(false); }
  }

  async function start(mode: "rules" | "byok") {
    if (!flow.importId) return;
    setBusy(true);
    setErr(null);
    try {
      const profile = await saveProfiles();
      if (!profile) return;
      const a = await api.post<AnalysisView>("/analyses", { import_id: flow.importId, profile, mode });
      if (activeRelationship.current !== flow.relationshipId) return;
      setFlow({ analysisId: a.id, mode });
      if (mode === "byok" && a.needs_confirmation) nav(`/preview/${a.id}`);
      else nav(`/progress/${a.id}`);
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  const hasKey = settings?.provider.ready ?? (settings?.provider.has_key || settings?.provider.preset === "ollama");
  const loading = loaded !== flow.relationshipId;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">人物档案与目标</h1>
        <p className="text-sm text-ink-2 mt-1">
          MBTI / 星座 / 属相与生日都可以留空。真实聊天证据优先，辅助标签在画像假设里的占比默认 15%（可在设置的「个性化权重」里调整或关掉），<strong>不进入客观关系分数</strong>。历法信息不等于性格预测依据。
          {rel && <span className="block mt-1 text-ink-3">当前对象：{rel.name}。双方资料保存到本机档案，切换好友时独立加载；每次分析保留当时版本。</span>}
        </p>
      </div>
      <fieldset disabled={busy || loading} className="grid gap-4 md:grid-cols-2">
        {(["me", "other"] as const).map((who) => (
          <Card key={who} title={who === "me" ? "我" : "对方"}>
            <div className="grid gap-3 grid-cols-2">
              <Field label="MBTI">
                <select className="input" value={p[who].mbti ?? ""} onChange={(e) => upd(who, "mbti", e.target.value)}>
                  <option value="">不填 / 不知道</option>
                  {MBTI_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="星座">
                <select className="input" value={p[who].zodiac ?? ""} onChange={(e) => upd(who, "zodiac", e.target.value)}>
                  <option value="">不填</option>
                  {ZODIAC.map(([v, l]) => <option key={v} value={v}>{l}座</option>)}
                </select>
              </Field>
              <Field label="属相">
                <select className="input" value={p[who].chinese_zodiac ?? ""} onChange={(e) => upd(who, "chinese_zodiac", e.target.value)}>
                  <option value="">不填</option>
                  {CHINESE_ZODIAC.map(([v, l]) => <option key={v} value={v}>属{l}</option>)}
                </select>
              </Field>
              <Field label="资料来源">
                <select className="input" value={p[who].source ?? "unknown"} onChange={(e) => upd(who, "source", e.target.value)}>
                  <option value="unknown">未注明</option><option value="user">本人填写 / 对方告知</option><option value="imported">导入资料</option>
                </select>
              </Field>
              <div className="col-span-2 border-t border-line pt-3 space-y-3">
                <Field label="出生日期（可只填年、年月或月日）" hint="不知道的部分留空；不会补造生日或自动推断星座、属相。">
                  <div className="grid grid-cols-3 gap-2">
                    <input className="input" type="number" min="1" max="9999" aria-label={`${who === "me" ? "我" : "对方"}的出生年份`} placeholder="年（未知留空）" value={p[who].birthday?.year ?? ""} onChange={(e) => birthdayPart(who, "year", e.target.value)} />
                    <input className="input" type="number" min="1" max="12" aria-label={`${who === "me" ? "我" : "对方"}的出生月份`} placeholder="月" value={p[who].birthday?.month ?? ""} onChange={(e) => birthdayPart(who, "month", e.target.value)} />
                    <input className="input" type="number" min="1" max="31" aria-label={`${who === "me" ? "我" : "对方"}的出生日期`} placeholder="日" value={p[who].birthday?.day ?? ""} onChange={(e) => birthdayPart(who, "day", e.target.value)} />
                  </div>
                </Field>
                <Field label="历法">
                  <select className="input" value={p[who].birthday?.calendar ?? "gregorian"} onChange={(e) => upd(who, "birthday", { precision: "unknown", ...p[who].birthday, calendar: e.target.value, is_leap_month: null })}>
                    <option value="gregorian">公历</option><option value="lunar">农历（按原样记录，暂不换算）</option>
                  </select>
                </Field>
                {p[who].birthday?.calendar === "lunar" && <label className="flex gap-2 items-center text-sm"><input type="checkbox" checked={p[who].birthday?.is_leap_month === true} onChange={(e) => upd(who, "birthday", { ...p[who].birthday, is_leap_month: e.target.checked })} />所填月份是闰月</label>}
                <label className="flex gap-2 items-center text-sm"><input type="checkbox" checked={p[who].confirmed ?? false} onChange={(e) => upd(who, "confirmed", e.target.checked)} />我已核对这些资料</label>
                <p className="text-xs text-ink-3">档案版本 {p[who].revision ?? 0} · {p[who].confirmed ? "已确认" : "尚未确认"}</p>
              </div>
            </div>
          </Card>
        ))}
      </fieldset>
      <div className="flex items-center gap-3">
        <button className="btn btn-primary" disabled={busy || loading} onClick={save}>{loading ? "正在加载档案…" : busy ? "正在保存…" : "保存双方档案"}</button>
        {saved && <span className="text-sm text-ink-2" role="status">已保存到本机</span>}
      </div>
      <Card title="这次复盘想解决什么？">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="目标">
            <select className="input" value={p.goal} onChange={(e) => setFlow({ profile: { ...p, goal: e.target.value as Goal } })}>
              {Object.entries(GOAL_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </Field>
          <Field label="关系状态（可选）" hint="只有你明确知道双方已确认关系时才勾选；否则由材料判断。">
            <label className="flex items-center gap-2 text-sm mt-2">
              <input type="checkbox" checked={p.stage_confirmed === "mutually_confirmed"} onChange={(e) => setFlow({ profile: { ...p, stage_confirmed: e.target.checked ? "mutually_confirmed" : null } })} />
              我们已经是确认的恋爱关系
            </label>
          </Field>
        </div>
        <Field label="补充说明（≤500 字，AI 模式会随脱敏正文一起发送；规则模式不使用）">
          <textarea className="input min-h-[80px]" maxLength={500} value={p.goal_text} onChange={(e) => setFlow({ profile: { ...p, goal_text: e.target.value } })} placeholder="例如：上周我提了一次见面，对方说改天，之后回复变少了。" />
        </Field>
      </Card>
      <Card title="选择分析方式">
        {!flow.importId && <Alert tone="info">档案可以先保存。开始分析前请先导入并确认材料。<a href="/import">去导入</a></Alert>}
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-lg border border-line p-3">
            <h3 className="font-medium">规则模式（本地，免费）</h3>
            <p className="text-sm text-ink-2 mt-1">回应 / 主动 / 投入五维分、趋势、阶段与边界判断、通用行动骨架。情感温度与未来指向只用词表估计，性格侧写不可用。</p>
            <button className="btn btn-primary mt-3" disabled={busy || loading || !flow.importId} onClick={() => start("rules")}>开始规则分析</button>
          </div>
          <div className="rounded-lg border border-line p-3">
            <h3 className="font-medium">AI 分析（自带 Key）</h3>
            <p className="text-sm text-ink-2 mt-1">
              发送脱敏后的聊天到你配置的模型（当前：{settings ? `${settings.provider.preset} / ${settings.provider.model}` : "…"}），得到带证据的观察、沟通侧写、个性化攻略。下一步会先预览发送内容与估价。
            </p>
            {!hasKey && <Alert tone="warn">尚未配置 API Key。<a href="/settings">去设置</a></Alert>}
            <button className="btn btn-primary mt-3" disabled={busy || loading || !hasKey || !flow.importId} onClick={() => start("byok")}>预览并确认出网</button>
          </div>
        </div>
      </Card>
      <ErrorBox error={err} />
      {!!err && <button className="btn btn-ghost" disabled={busy} onClick={() => setReload((n) => n + 1)}>重新载入已保存档案</button>}
    </div>
  );
}
