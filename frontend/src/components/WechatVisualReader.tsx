import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Monitor, ScanText, ShieldCheck } from "lucide-react";
import { api, ApiError } from "@/api/client";
import type { ImportView, Message } from "@/api/types";
import { RelationshipPicker } from "@/components/RelationshipPicker";
import { Alert, Badge, Button, Card, ErrorBox, Field, Spinner } from "@/components/ui";
import { rememberWechatBinding } from "@/lib/wechat";
import { useFlow } from "@/store";

interface ReaderConfig { windows: boolean; automation: boolean; ocr: boolean; enabled: boolean; allow_fill: boolean; available: boolean; note: string; ocr_engine?: { model: string; device: string; local: boolean } }
interface HistorySummary { requested: number; collected: number; pages: number; overlap_removed: number; stop_reason: string; me: number; other: number; unknown: number; text_characters: number; known_times: number; label_times?: number; interval_pairs: number; median_interval_seconds: number | null; returned_to_latest: boolean; media?: { image: number; file: number }; media_text_removed?: number; ocr_devices?: Record<string, number> }
interface CapturePreview { token: string; contact: string; relationship_id: string; expires_in_s: number; image: string; messages: Message[]; warnings: { code: string; msg: string }[]; history?: HistorySummary | null }
interface ReadProgress { phase: string; collected: number; target: number; pages: number; device?: string }
interface HistoryStatus { status: string; progress: ReadProgress; result: CapturePreview | null; error: { code: string; msg: string; hint: string } | null }
const PHASES: Record<string, string> = { opening: "正在打开指定好友", latest: "正在向下滚动，定位最新消息", reading: "正在向上滚动并识别历史", restoring: "正在返回最新消息", stopping: "正在停止，整理已读取内容" };
const REASONS: Record<string, string> = { target_reached: "已达到目标", no_more_loaded_history: "多次滚动后未加载更多历史", cancelled: "已按你的操作停止", time_limit: "达到本次读取时间上限", page_limit: "达到本次翻页上限", wechat_history_overlap: "重叠内容无法可靠拼接", wechat_history_no_text: "遇到无法识别的文字区域", wechat_focus_changed: "窗口焦点发生变化", wechat_window_obscured: "微信被其他窗口遮挡", wechat_window_unavailable: "微信界面在读取中途发生变化" };

export function WechatVisualReader() {
  const nav = useNavigate();
  const [flow, setFlow] = useFlow();
  const [config, setConfig] = useState<ReaderConfig | null>(null);
  const [contact, setContact] = useState("");
  const [capture, setCapture] = useState<CapturePreview | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [targetCount, setTargetCount] = useState(1000);
  const [progress, setProgress] = useState<ReadProgress | null>(null);
  const jobRef = useRef<string | null>(null);
  const [previewPage, setPreviewPage] = useState(0);
  const currentKey = JSON.stringify([contact, flow.relationshipId, targetCount]);
  const latestKey = useRef(currentKey); latestKey.current = currentKey;
  useEffect(() => {
    let alive = true;
    api.get<ReaderConfig>("/wechat/config").then(value => { if (alive) setConfig(value); }).catch(value => { if (alive) setError(value); });
    return () => { alive = false; latestKey.current = "unmounted"; if (jobRef.current) void api.post(`/wechat/history/${jobRef.current}/cancel`, {}).catch(() => {}); };
  }, []);
  useEffect(() => { setCapture(null); setPreviewPage(0); if (jobRef.current) void api.post(`/wechat/history/${jobRef.current}/cancel`, {}).catch(() => {}); }, [currentKey]);

  async function read() {
    if (!contact.trim() || targetCount < 10 || targetCount > 5000 || !Number.isInteger(targetCount)) return;
    const key = currentKey;
    setBusy("正在读取历史聊天…"); setError(null); setCapture(null); setProgress({ phase: "opening", collected: 0, target: targetCount, pages: 0 });
    try {
      const job = await api.post<{ id: string }>("/wechat/history", { contact: contact.trim(), relationship_id: flow.relationshipId, target_count: targetCount });
      jobRef.current = job.id;
      while (latestKey.current === key) {
        const status = await api.get<HistoryStatus>(`/wechat/history/${job.id}`);
        if (latestKey.current !== key) break;
        setProgress(status.progress?.phase ? status.progress : { phase: "opening", collected: 0, target: targetCount, pages: 0 });
        if (status.status !== "running") {
          if (status.error) throw new ApiError(409, status.error.code, status.error.msg, status.error.hint);
          if (status.result) { setCapture(status.result); setPreviewPage(0); }
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
      if (latestKey.current !== key) await api.post(`/wechat/history/${job.id}/cancel`, {});
    } catch (value) {
      if (jobRef.current) await api.post(`/wechat/history/${jobRef.current}/cancel`, {}).catch(() => {});
      if (latestKey.current === key) setError(value);
    } finally {
      jobRef.current = null; setBusy(""); setProgress(null);
      if (latestKey.current !== "unmounted") void api.get<ReaderConfig>("/wechat/config").then(setConfig).catch(() => {});
    }
  }
  async function stop() { if (jobRef.current) { await api.post(`/wechat/history/${jobRef.current}/cancel`, {}).catch(setError); setProgress(value => value && { ...value, phase: "stopping" }); } }
  async function useCapture(next: "analysis" | "reply") {
    if (!capture) return;
    const key = currentKey;
    setBusy("导入识别结果…"); setError(null);
    try {
      const imported = await api.post<ImportView>("/wechat/import", { token: capture.token, relationship_id: flow.relationshipId });
      if (latestKey.current !== key) return;
      rememberWechatBinding({ token: capture.token, import_id: imported.id, relationship_id: imported.relationship_id });
      setFlow({ importId: imported.id, analysisId: null, reportId: null });
      nav(next === "reply" ? "/review?next=replies" : "/review");
    } catch (value) { setError(value); } finally { setBusy(""); }
  }
  return <div className="space-y-5">
    <Card title="画面读取设置" right={<Badge tone={config?.available ? "good" : "warn"}>{config?.available ? "本机组件就绪" : "检查本机组件"}</Badge>}>
      <div className="flex flex-wrap gap-3 text-xs mb-4"><span className="chip"><Monitor size={13} /> Windows 无障碍</span><span className="chip"><ScanText size={13} /> 本地 OCR</span><span className="chip"><ShieldCheck size={13} /> 不读取微信数据库</span></div>
      {config?.ocr_engine && <p className="mb-3 text-xs text-ink-2">{config.ocr_engine.model} · {config.ocr_engine.device} · 聊天图片在本机识别</p>}
      {config && !config.windows && <Alert tone="warn">当前系统不是 Windows，无法读取微信桌面窗口。仍可使用“导入对话”中的截图导入。</Alert>}
      {config?.windows && !config.available && <Alert tone="warn">桌面或 OCR 扩展尚未就绪。在后端目录安装 <code className="mono">uv sync --extra desktop --extra ocr</code> 后重启服务。</Alert>}
      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
        <Badge tone={config?.enabled ? "good" : "neutral"}>{config?.enabled ? "读取已开启" : "读取未开启"}</Badge>
        <Badge tone={config?.allow_fill ? "good" : "neutral"}>{config?.allow_fill ? "允许填入 / 发送" : "仅读取，不操作微信"}</Badge>
        <Link className="underline text-ink-2" to="/settings">在「设置 → 微信与桌面」里调整 →</Link>
      </div>
      <p className="mt-3 text-xs text-ink-3">每次读取由你点击发起。填入和发送均绑定本次已核对的好友；发送还需核对具体内容并单独确认。截图不会保存到磁盘。</p>
    </Card>
    <Card title="指定一位好友" hint="输入完整备注或昵称；窗口查找、好友搜索和聊天区域定位由心迹完成。">
      <fieldset disabled={!!busy || !config?.enabled} className="space-y-4">
        <RelationshipPicker label="保存到哪个关系对象" />
        <Field label="好友完整备注 / 昵称"><input className="input" value={contact} maxLength={80} placeholder="例如：好友的完整微信备注" onChange={event => setContact(event.target.value)} autoComplete="off" /></Field>
        <Field label="读取最近多少条文字消息" hint="10–5000 条，例如 1000。以识别后的独立文字消息计数；图片、语音不计入。"><input className="input" type="number" min={10} max={5000} step={1} value={targetCount} onChange={event => setTargetCount(Number(event.target.value))} /><div className="mt-2 flex gap-2">{[100, 300, 1000].map(count => <Button key={count} size="sm" onClick={() => setTargetCount(count)}>{count} 条</Button>)}</div></Field>
        <div className="flex flex-wrap gap-2 text-xs text-ink-2" aria-label="自动读取流程"><span className="chip">① 找到微信</span><span className="chip">② 打开指定好友</span><span className="chip">③ 自动定位聊天</span><span className="chip">④ 本机识别并校对</span></div>
        <p className="text-xs text-ink-3">所需时间取决于历史数量和电脑性能，1000 条可能需要较长时间。期间微信会自动滚动，请暂时不要操作鼠标键盘；可随时停止并查看已读部分。没有显示逐条时间的消息，不会用于推算回复间隔。</p>
        <Button variant="primary" disabled={!contact.trim() || !!busy || targetCount < 10 || targetCount > 5000 || !Number.isInteger(targetCount)} onClick={read}><ScanText size={16} /> 自动回溯并读取</Button>
      </fieldset>
    </Card>
    {progress ? <Card title={PHASES[progress.phase] ?? "正在读取"} right={<Button disabled={progress.phase === 'stopping'} onClick={stop}>停止读取</Button>}><p role="status" aria-live="polite">已读取 {progress.collected} / {progress.target} 条 · {progress.pages} 段画面{progress.device ? ` · 识别设备 ${progress.device}` : ''}</p><progress className="mt-3 w-full accent-[#a76b91]" max={progress.target} value={progress.collected} aria-label="历史读取进度" /></Card> : busy && <Spinner label={busy} />}<ErrorBox error={error} />
    {capture && <Card title={`已核对：${capture.contact}`} hint="历史按时间先后排列。进入校对页核对角色、文字和时间后再使用。" right={<Badge tone="good">{capture.messages.length} 条文字消息</Badge>}>
      {capture.history && <div className="mb-5 space-y-3"><Alert tone={capture.history.stop_reason === 'target_reached' ? 'info' : 'warn'}>{REASONS[capture.history.stop_reason] ?? '读取已中断，保留已读部分'}：目标 {capture.history.requested} 条，实际 {capture.history.collected} 条。</Alert><div className="flex flex-wrap gap-3 text-sm"><Badge>我 {capture.history.me} 条</Badge><Badge>对方 {capture.history.other} 条</Badge><Badge>{capture.history.text_characters} 字符</Badge><Badge>{capture.history.pages} 段画面</Badge><Badge>排除 {capture.history.overlap_removed} 次重叠识别</Badge>{capture.history.media && <Badge>图片 {capture.history.media.image} · 文件 {capture.history.media.file}（占位，不计条数）</Badge>}</div>{capture.history.ocr_devices && <p className="text-xs text-ink-3">识别设备：{Object.entries(capture.history.ocr_devices).map(([device, pages]) => `${device} ${pages} 段`).join('，')}{capture.history.media_text_removed ? ` · 图片和文件内的 ${capture.history.media_text_removed} 处文字未作为聊天内容` : ''}</p>}<p className="text-sm text-ink-2">明确时间 {capture.history.known_times} / {capture.history.collected} 条{capture.history.label_times ? ` · 按时间标签定位 ${capture.history.label_times} 条` : ''} · 可计算相邻间隔 {capture.history.interval_pairs} 组{capture.history.median_interval_seconds !== null ? ` · 间隔中位数约 ${Math.round(capture.history.median_interval_seconds / 60)} 分钟` : ' · 证据不足，暂不计算间隔'}。</p>{!capture.history.returned_to_latest && <p className="text-xs text-ink-3">本次未返回最新消息。填入回复前请重新读取，避免使用过时的上下文。</p>}</div>}
      <div className="grid gap-5 lg:grid-cols-2"><div><img className="wechat-capture-image" src={capture.image} alt="开始读取时的最新聊天画面" /><p className="text-xs text-ink-3 mt-2">这是开始读取时的最新画面；历史识别结果在右侧按从旧到新排列。</p></div><div><div className="wechat-ocr-list">{capture.messages.slice(previewPage * 50, (previewPage + 1) * 50).map(message => <div key={message.id} className={`wechat-ocr-message ${message.sender === 'me' ? 'is-me' : ''}`}><Badge tone={message.sender === 'me' ? 'brand' : 'lavender'}>{message.sender === 'me' ? '我' : message.sender === 'other' ? '对方' : '待核对'}</Badge><p>{message.text}</p><p className="text-xs text-ink-3 mt-1">{message.ts ? `显示时间：${message.ts.replace('T', ' ')}` : typeof message.meta?.time_label === 'string' ? `所在时间段：${message.meta.time_label}（本条时间未知）` : '本条时间未知'}</p></div>)}</div>{capture.messages.length > 50 && <div className="mt-3 flex items-center justify-between"><Button size="sm" disabled={!previewPage} onClick={() => setPreviewPage(value => value - 1)}>上一页</Button><span className="text-xs">{previewPage + 1} / {Math.ceil(capture.messages.length / 50)}</span><Button size="sm" disabled={(previewPage + 1) * 50 >= capture.messages.length} onClick={() => setPreviewPage(value => value + 1)}>下一页</Button></div>}</div></div><div className="mt-4 space-y-2">{capture.warnings.map(warning => <p key={warning.code + warning.msg} className="text-xs text-ink-3">{warning.msg}</p>)}</div><div className="mt-5 flex flex-wrap gap-3"><Button disabled={!!busy} onClick={() => useCapture('analysis')}>导入并校对分析</Button><Button variant="primary" disabled={!!busy} onClick={() => useCapture('reply')}>校对后生成代回复</Button></div><p className="text-xs text-ink-3 mt-3">好友窗口绑定有效期约 {Math.round(capture.expires_in_s/60)} 分钟，从读取结束开始计时。</p>
    </Card>}
  </div>;
}
