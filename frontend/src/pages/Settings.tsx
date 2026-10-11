import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { DriveConfig, SettingsView, StatsView } from "@/api/types";
import { Alert, Badge, Card, ErrorBox, Field, Spinner, Tabs } from "@/components/ui";
import { LocalModelsCard, TASK_LABELS } from "@/components/LocalModels";
import { PriorPolicyCard } from "@/components/PriorPolicyCard";
import { EmotionModelCard } from "@/components/EmotionModelCard";
import { MODE_LABELS } from "@/i18n/labels";

interface ReportRow { id: string; created_at: string; mode: string; total: number | null; relationship_id: string }
interface DshConfig { enabled: boolean; available: boolean; connected: string | null; note: string }
interface WxVisualConfig { windows: boolean; automation: boolean; ocr: boolean; enabled: boolean; allow_fill: boolean; available: boolean; note: string }

const TABS = [
  { id: "model", label: "模型与连接" },
  { id: "analysis", label: "分析与个性化" },
  { id: "drive", label: "主动提醒" },
  { id: "wechat", label: "微信与桌面" },
  { id: "data", label: "数据与诊断" },
];

/** 设置模块：所有开关与偏好集中在这里，按用途分组；页面上只留与当前任务直接相关的按钮。 */
export function SettingsPage() {
  const [tab, setTab] = useState("model");
  const [s, setS] = useState<SettingsView | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [preset, setPreset] = useState("deepseek");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [key, setKey] = useState("");
  const [warn, setWarn] = useState("0.5");
  const [mode, setMode] = useState<"local" | "cloud">("local");
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [diag, setDiag] = useState<Record<string, unknown> | null>(null);
  const [wiped, setWiped] = useState<{ cleared: Record<string, number>; not_covered: string[] } | null>(null);
  const [stats, setStats] = useState<StatsView | null>(null);
  const [drive, setDrive] = useState<DriveConfig | null>(null);
  const [desk, setDesk] = useState<{ available: boolean; enabled: boolean; allow_send: boolean; note: string } | null>(null);
  const [dsh, setDsh] = useState<DshConfig | null>(null);
  const [wxv, setWxv] = useState<WxVisualConfig | null>(null);
  const [samples, setSamples] = useState<number | null>(null);

  async function load() {
    const v = await api.get<SettingsView>("/settings");
    setS(v);
    setPreset(v.provider.preset);
    setModel(v.provider.model);
    setBaseUrl(v.provider.base_url ?? "");
    setWarn(String(v.provider.cost_warn_cny));
    setMode(v.connection_mode);
    setReports(await api.get<ReportRow[]>("/reports"));
    setStats(await api.get<StatsView>("/stats?days=30").catch(() => null));
    setDrive(await api.get<DriveConfig>("/drive/config").catch(() => null));
    setDesk(await api.get<{ available: boolean; enabled: boolean; allow_send: boolean; note: string }>("/desktop/config").catch(() => null));
    setDsh(await api.get<DshConfig>("/wechat/dsh/config").catch(() => null));
    setWxv(await api.get<WxVisualConfig>("/wechat/config").catch(() => null));
    setSamples(await api.get<{ k: number }>("/diary/intent-samples/state").then((v) => v.k).catch(() => null));
  }
  useEffect(() => { load().catch(setErr); }, []);

  const presetInfo = s?.presets.find((p) => p.id === preset);
  const localPreset = presetInfo?.native === "ollama";

  async function save() {
    setBusy(true);
    setErr(null);
    setTest(null);
    try {
      await api.put("/settings", { preset, model: model || null, base_url: preset === "custom" ? baseUrl || null : null, api_key: key === "" ? null : key, cost_warn_cny: parseFloat(warn), connection_mode: mode });
      setKey("");
      await load();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  if (!s) return err ? <ErrorBox error={err} /> : <Spinner />;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">设置与数据</h1>
      <p className="text-sm text-ink-3">
        所有开关和偏好集中在这里，按用途分成 5 组；页面上只保留与当前任务直接相关的按钮。
        下面的改动除标注「保存」的以外都即时生效。
      </p>
      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {tab === "model" && (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="模型供应商（BYOK）">
            <div className="space-y-3">
              <Field label="连接模式" hint={s.effective_chain}>
                <div className="flex flex-wrap gap-3 text-sm">
                  <label className="flex items-center gap-1"><input type="radio" name="mode" checked={mode === "local"} onChange={() => setMode("local")} /> 本地：只允许本机端点，配置云端会被直接拒绝</label>
                  <label className="flex items-center gap-1"><input type="radio" name="mode" checked={mode === "cloud"} onChange={() => setMode("cloud")} /> 云端：每次出网前预览、哈希确认</label>
                </div>
                {mode === "cloud" && localPreset && <span className="block text-xs text-warn mt-1">云端模式下选了本机 Ollama 预设，实际仍走本机。</span>}
                {mode === "local" && !localPreset && preset !== "custom" && <span className="block text-xs text-warn mt-1">本地模式下选了云端预设：分析会报 mode_violation（回复草稿会改用本机风格检索）。要用它请切到云端。</span>}
              </Field>
              <Field label="预设">
                <select className="input" value={preset} onChange={(e) => { setPreset(e.target.value); const p = s.presets.find((x) => x.id === e.target.value); if (p) setModel(p.default_model); }}>
                  {s.presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
              </Field>
              {presetInfo && (
                <p className="text-xs text-ink-3">
                  接入地域：{presetInfo.endpoint_region} · 处理范围：{presetInfo.processing_scope} · 价格{presetInfo.price.input_per_m === null ? "未知" : `：输入 ¥${presetInfo.price.input_per_m}/M，输出 ¥${presetInfo.price.output_per_m}/M（${presetInfo.price.verified_on} 核对）`}
                </p>
              )}
              <Field label="模型" hint={presetInfo?.task_models ? `此档位分工：${Object.entries(presetInfo.task_models).map(([t, m]) => `${TASK_LABELS[t] ?? t} ${m}`).join(" · ")}。这里改成别的模型名会对四类任务统一生效。` : undefined}><input className="input mono" value={model} onChange={(e) => setModel(e.target.value)} /></Field>
              {preset === "custom" && <Field label="base_url（仅 https，或本机 http）"><input className="input mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" /></Field>}
              <Field label={`API Key ${s.provider.has_key ? `（已保存，来源：${s.provider.key_source === "env" ? "环境变量 RAPPORT_API_KEY" : "本机加密文件"}）` : "（未配置）"}`} hint="Key 加密保存在本机数据目录，不会出现在页面、日志、导出或诊断信息中。留空表示不修改。">
                <input className="input mono" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder={s.provider.has_key ? "••••••••（不修改）" : "sk-…"} />
              </Field>
              <Field label="单份报告费用预警阈值（元）" hint="预计费用超过这个数时，出网前会要求你单独确认。"><input className="input w-32" type="number" step="0.1" min="0" value={warn} onChange={(e) => setWarn(e.target.value)} /></Field>
              <div className="flex gap-2 items-center">
                <button className="btn btn-primary" disabled={busy} onClick={save}>保存</button>
                <button className="btn btn-ghost" disabled={busy || !(s.provider.has_key || localPreset)} onClick={async () => { setBusy(true); try { setTest(await api.post("/settings/test-provider")); } catch (e) { setErr(e); } finally { setBusy(false); } }}>测试连通性</button>
                {test && <Badge tone={test.ok ? "good" : "warn"}>{test.ok ? "连接成功" : test.message}</Badge>}
              </div>
            </div>
          </Card>
          <LocalModelsCard currentPreset={s.provider.preset} onSwitched={load} />
        </div>
      )}

      {tab === "analysis" && (
        <div className="grid gap-4 md:grid-cols-2">
          <PriorPolicyCard onSaved={load} />
          <EmotionModelCard onSaved={load} />
          <Card title="语义判断（AI 模式）">
            <div className="space-y-2 text-sm">
              <p className="text-xs text-ink-3">
                AI 分析对每句话的情绪、语气做多次判断时，这里决定每处判断让模型答几次。
              </p>
              <Field label="每处判断采样次数" hint="1 = 单次调用（默认）。3 = 三次采样取多数，费用约 3 倍；三次全一致的负向判断可以上浮为结论。它衡量的是模型的确定，不是正确。">
                <select className="input" value={samples ?? 1} onChange={async (e) => { const v = await api.put<{ k: number }>(`/diary/intent-samples/state?k=${e.target.value}`); setSamples(v.k); }}>
                  {[1, 3, 5].map((n) => <option key={n} value={n}>{n} 次</option>)}
                </select>
              </Field>
              <p className="text-xs text-ink-3">真实材料上的实测（作者材料，弱金口径）：单次上线后 macro-F1 0.30，三次采样 0.44，但把 10% 固定词表无类的句子误判为负向。详见 docs/EVAL.md §15。</p>
            </div>
          </Card>
        </div>
      )}

      {tab === "drive" && (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="主动提醒（激素系统）">
            {!drive ? <p className="text-sm text-ink-3">读取中…</p> : (
              <div className="space-y-3 text-sm">
                <label className="flex items-center gap-3 rounded-lg border border-line p-3">
                  <input type="checkbox" checked={drive.enabled} onChange={async (e) => setDrive(await api.patch<DriveConfig>("/drive/config", { enabled: e.target.checked }))} />
                  <span className="flex-1">
                    <b>开启主动提醒</b>
                    <span className="block text-xs text-ink-3">
                      默认关闭；关闭后不会产生任何主动提醒。开启后按「冲动 / 抑制 / 刺激」三个可解释的状态量，
                      偶尔在收件箱里主动提起对方、提示回复节奏或推一条你批准过的话术。只发给你自己，不碰微信；
                      对方有停止联系或安全信号时只会关心你自己。
                    </span>
                  </span>
                  <Badge tone={drive.enabled ? "good" : "neutral"}>{drive.enabled ? "已开启" : "已关闭"}</Badge>
                </label>
                <Field label="每天最多" hint="一天最多在收件箱里出现几条提醒；0 条表示不提醒（等同关闭）。">
                  <select className="input" value={drive.daily_cap} onChange={async (e) => setDrive(await api.patch<DriveConfig>("/drive/config", { daily_cap: Number(e.target.value) }))}>
                    {[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n} 条</option>)}
                  </select>
                </Field>
                <Field label="静默时段" hint="这段时间不提醒（按本机时钟，0–23 点）。">
                  <div className="flex items-center gap-2">
                    <input className="input w-20" type="number" min={0} max={23} value={drive.quiet_start_h} onChange={async (e) => setDrive(await api.patch<DriveConfig>("/drive/config", { quiet_start_h: Number(e.target.value) }))} />
                    <span>点 至</span>
                    <input className="input w-20" type="number" min={0} max={23} value={drive.quiet_end_h} onChange={async (e) => setDrive(await api.patch<DriveConfig>("/drive/config", { quiet_end_h: Number(e.target.value) }))} />
                    <span>点</span>
                  </div>
                </Field>
                <p className="text-xs text-ink-3">参数版本 <span className="mono">{drive.version}</span>。<Link to="/inbox">打开收件箱</Link></p>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === "wechat" && (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="微信数据读取（本机数据库，默认关闭）">
            {!dsh ? <p className="text-sm text-ink-3">读取中…</p> : (
              <div className="space-y-2 text-sm">
                <label className="flex items-center gap-3 rounded-lg border border-line p-3">
                  <input type="checkbox" checked={dsh.enabled} disabled={!dsh.available} onChange={async (e) => setDsh(await api.patch<DshConfig>("/wechat/dsh/config", { enabled: e.target.checked }))} />
                  <span className="flex-1">
                    <b>开启数据库读取</b>
                    <span className="block text-xs text-ink-3">在本机直接读取微信聊天记录（不外发）。具体读哪个好友、读多少条、要不要把语音转成文字，在「微信聊天读取」页按次操作。</span>
                  </span>
                  <Badge tone={dsh.enabled ? "good" : "neutral"}>{dsh.enabled ? "已开启" : "已关闭"}</Badge>
                </label>
                {!dsh.available && <Alert tone="warn">此功能需要 Windows 本机微信。仍可使用导入对话功能。</Alert>}
                {dsh.note && <p className="text-xs text-ink-3">{dsh.note}</p>}
                <p className="text-xs text-ink-3"><Link to="/wechat">打开「微信聊天读取」页 →</Link></p>
              </div>
            )}
          </Card>
          <Card title="画面读取（备用方式，Windows，默认关闭）">
            {!wxv ? <p className="text-sm text-ink-3">读取中…</p> : (
              <div className="space-y-2 text-sm">
                <p className="text-xs text-ink-3">
                  不读数据库：通过 Windows 无障碍接口 + 本机 OCR 识别微信窗口里的聊天，作为数据库读取的备用方案。{wxv.note}
                </p>
                <label className="flex items-center gap-2"><input type="checkbox" checked={wxv.enabled} disabled={!wxv.available} onChange={async (e) => setWxv(await api.patch<WxVisualConfig>("/wechat/config", { enabled: e.target.checked }))} /> 开启微信可见聊天读取</label>
                <label className={`flex items-center gap-2 ${wxv.enabled ? "" : "opacity-50"}`}><input type="checkbox" disabled={!wxv.enabled} checked={wxv.allow_fill} onChange={async (e) => setWxv(await api.patch<WxVisualConfig>("/wechat/config", { allow_fill: e.target.checked }))} /> 允许确认后操作微信（填入草稿或发送；每条仍需你单独确认）</label>
                {!wxv.windows && <Alert tone="warn">当前系统不是 Windows，无法读取微信桌面窗口。仍可使用「导入对话」中的截图导入。</Alert>}
                {wxv.windows && !wxv.available && <Alert tone="warn">桌面或 OCR 扩展尚未就绪。在后端目录安装 <code className="mono">uv sync --extra desktop --extra ocr</code> 后重启服务。</Alert>}
                <p className="text-xs text-ink-3"><Link to="/wechat">打开「微信聊天读取」页 →</Link>（读取哪位好友、读多少条在该页按次选择）</p>
              </div>
            )}
          </Card>
          <Card title="粘贴到聊天窗口（Windows，默认关闭）">
            {!desk ? <p className="text-sm text-ink-3">读取中…</p> : !desk.available ? (
              <p className="text-sm text-ink-3">本机未安装桌面扩展。在 backend 目录运行 <span className="mono">uv sync --extra desktop</span> 后重启即可（仅 Windows）。</p>
            ) : (
              <div className="space-y-2 text-sm">
                <p className="text-xs text-ink-3">{desk.note} 每个聊天软件的接口都不同，这里用的是 Windows 给所有应用的同一条通道：无障碍树里当前聚焦的可输入控件。它不读取任何聊天内容，只往你选中的输入框里粘贴。</p>
                <label className="flex items-center gap-2"><input type="checkbox" checked={desk.enabled} onChange={async (e) => setDesk(await api.patch("/desktop/config", { enabled: e.target.checked }))} /> 允许把草稿粘贴到我当前聚焦的输入框（不回车）</label>
                <label className={`flex items-center gap-2 ${desk.enabled ? "" : "opacity-50"}`}><input type="checkbox" disabled={!desk.enabled} checked={desk.allow_send} onChange={async (e) => setDesk(await api.patch("/desktop/config", { allow_send: e.target.checked }))} /> 允许「粘贴并发送」（按回车）。每条仍需你单独点一次；请先确认这条话术你愿意发出去</label>
                <p className="text-xs text-ink-3">用法：在代回复页点「粘贴到聊天窗口」，3 秒内把光标点进微信/QQ 的输入框。粘贴记录只存窗口标题与控件类型，不存文字。</p>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === "data" && (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="本机数据">
            <dl className="text-sm grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              <dt className="text-ink-3">数据目录</dt><dd className="mono text-xs break-all">{s.data_dir}</dd>
              <dt className="text-ink-3">原始聊天保留</dt><dd>分析完成后 {s.limits.raw_retention_hours} 小时自动清理（加密存储）</dd>
              <dt className="text-ink-3">报告保留</dt><dd>直到你删除</dd>
              <dt className="text-ink-3">截图 OCR</dt><dd>{s.ocr_available ? "可用" : "未安装"}</dd>
              {s.schema_changes && Object.keys(s.schema_changes).length > 0 && <><dt className="text-ink-3">本次启动升级</dt><dd className="text-xs">已为旧数据库补齐字段：{Object.entries(s.schema_changes).map(([t, c]) => `${t}(${c.join(",")})`).join("；")}</dd></>}
            </dl>
            <div className="mt-3 flex gap-2">
              <button className="btn btn-danger" onClick={async () => { if (confirm("清空全部对象的导入、分析、报告、日记、反馈与调用记录？此操作不可撤销。")) { setWiped(await api.del("/data")); await load(); } }}>清空全部数据</button>
              <button className="btn btn-ghost" onClick={() => api.get<Record<string, unknown>>("/diagnostics").then(setDiag)}>生成诊断信息</button>
            </div>
            {wiped && <div className="mt-3"><Alert tone="good">已清除：{Object.entries(wiped.cleared).map(([k, v]) => `${k} ${v}`).join("，")}。不在清除范围：{wiped.not_covered.join("；")}</Alert></div>}
            {diag && <pre className="mono text-xs bg-paper rounded p-2 mt-3 overflow-auto">{JSON.stringify(diag, null, 2)}</pre>}
          </Card>
          <Card title="模型用量（最近 30 天，只记元数据）">
            {!stats || stats.llm.totals.calls === 0 ? <p className="text-sm text-ink-3">还没有模型调用记录。</p> : (
              <div className="text-sm">
                <dl className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  <div><dt className="text-xs text-ink-3">调用</dt><dd className="font-medium">{stats.llm.totals.calls}</dd></div>
                  <div><dt className="text-xs text-ink-3">tokens 入/出</dt><dd className="font-medium">{stats.llm.totals.prompt_tokens} / {stats.llm.totals.completion_tokens}</dd></div>
                  <div><dt className="text-xs text-ink-3">估算费用</dt><dd className="font-medium">¥{stats.llm.totals.cost_cny}</dd></div>
                  <div><dt className="text-xs text-ink-3">截断 / 失败率</dt><dd className={`font-medium ${stats.llm.totals.failure_rate > 0.05 ? "text-warn" : ""}`}>{(stats.llm.totals.truncation_rate * 100).toFixed(1)}% / {(stats.llm.totals.failure_rate * 100).toFixed(1)}%</dd></div>
                </dl>
                <table className="w-full text-xs mt-2">
                  <thead className="text-ink-3 text-left"><tr><th>用途</th><th>次数</th><th>平均耗时</th><th>截断</th><th>失败</th></tr></thead>
                  <tbody>
                    {Object.entries(stats.llm.by_purpose).map(([k, v]) => (
                      <tr key={k} className="border-t border-line"><td>{k}</td><td>{v.calls}</td><td>{(v.avg_latency_ms / 1000).toFixed(1)}s</td><td>{v.truncated}</td><td>{v.bad_json + v.errors}</td></tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-xs text-ink-3 mt-2">{stats.llm.note}</p>
                {stats.llm.totals.truncation_rate > 0.05 && <div className="mt-2"><Alert tone="warn">截断率偏高：推理型模型会先消耗思考 token，可考虑换非推理模型或缩小区间。</Alert></div>}
              </div>
            )}
          </Card>
          <Card title="已保存的报告">
            {reports.length === 0 ? <p className="text-sm text-ink-3">暂无</p> : (
              <ul className="text-sm divide-y divide-line">
                {reports.map((r) => (
                  <li key={r.id} className="flex items-center gap-3 py-2">
                    <Link to={`/report/${r.id}`} className="mono text-xs">{r.id}</Link>
                    <span className="text-ink-3">{new Date(r.created_at).toLocaleString()}</span>
                    <Badge>{MODE_LABELS[r.mode] ?? r.mode}</Badge>
                    {r.relationship_id !== "default" && <Badge tone="accent">{r.relationship_id}</Badge>}
                    <span className="ml-auto">{r.total ?? "—"}</span>
                    <button className="btn btn-ghost py-0.5 text-xs" onClick={async () => { await api.del(`/reports/${r.id}`); await load(); }}>删除</button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
      <ErrorBox error={err} />
    </div>
  );
}
