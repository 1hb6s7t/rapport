import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "@/api/client";
import type { ImportView, SettingsView } from "@/api/types";
import { Alert, Card, ErrorBox, Field, Spinner } from "@/components/ui";
import { RelationshipPicker } from "@/components/RelationshipPicker";
import { setFlow, useFlow } from "@/store";

const SAMPLE = `[2026-09-01 20:00] 我: 今天的活动你也在啊
[2026-09-01 20:15] 对方: 哈哈对，我坐后排
[2026-09-01 20:16] 我: 那下次可以聊聊
[2026-09-01 20:40] 对方: 可以呀，周末有空吗？`;

type Tab = "paste" | "file" | "ocr";

export function ImportPage() {
  const nav = useNavigate();
  const [flow] = useFlow();
  const rid = flow.relationshipId;
  const [tab, setTab] = useState<Tab>("paste");
  const [text, setText] = useState("");
  const [fmt, setFmt] = useState("auto");
  const [dateOrder, setDateOrder] = useState("");
  const [meName, setMeName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [meSide, setMeSide] = useState<"right" | "left">("right");
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState<SettingsView | null>(null);

  useEffect(() => {
    api.get<SettingsView>("/settings").then(setSettings).catch(() => {});
  }, []);

  function done(imp: ImportView) {
    setFlow({ importId: imp.id, analysisId: null, reportId: null });
    nav("/review");
  }

  async function submitText() {
    setBusy(true);
    setErr(null);
    try {
      done(await api.post<ImportView>("/imports", { text, format: fmt, date_order: dateOrder || null, me_name: meName || null, relationship_id: rid }));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }
  async function submitFile() {
    if (!files[0]) return;
    setBusy(true);
    setErr(null);
    try {
      const fd = new FormData();
      fd.append("file", files[0]);
      fd.append("format", fmt);
      if (dateOrder) fd.append("date_order", dateOrder);
      if (meName) fd.append("me_name", meName);
      fd.append("relationship_id", rid);
      done(await api.form<ImportView>("/imports/file", fd));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }
  async function submitOcr() {
    if (!files.length) return;
    setBusy(true);
    setErr(null);
    try {
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      fd.append("me_side", meSide);
      fd.append("relationship_id", rid);
      done(await api.form<ImportView>("/imports/ocr", fd));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  const ambiguous = err instanceof ApiError && err.code === "ambiguous_date";
  const limits = settings?.limits;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">导入聊天材料</h1>
        <p className="text-sm text-ink-2 mt-1">材料只在本机解析。下一步你可以逐条校正角色、时间与分析区间，然后再决定是否使用 AI。</p>
      </div>
      <Card>
        <RelationshipPicker />
      </Card>
      <div className="flex gap-1 border-b border-line" role="tablist">
        {(["paste", "file", "ocr"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={`px-4 py-2 text-sm -mb-px border-b-2 ${tab === t ? "border-ink font-medium" : "border-transparent text-ink-3"}`} onClick={() => { setTab(t); setErr(null); setFiles([]); }}>
            {{ paste: "粘贴文本", file: "导出文件", ocr: "聊天截图" }[t]}
          </button>
        ))}
      </div>

      {tab === "paste" && (
        <Card>
          <div className="space-y-3">
            <Field label="每行一条：「我: 内容」「对方: 内容」，也可以用 A:/B:，可带 [时间] 前缀" hint="支持直接粘贴 WhatsApp 导出文本、QQ 消息管理器导出文本或 message-v1 JSON，会自动识别。">
              <textarea className="input mono min-h-[260px]" value={text} onChange={(e) => setText(e.target.value)} placeholder={SAMPLE} />
            </Field>
            <Options fmt={fmt} setFmt={setFmt} dateOrder={dateOrder} setDateOrder={setDateOrder} meName={meName} setMeName={setMeName} formats={settings?.formats} showDate={ambiguous || dateOrder !== ""} />
            <div className="flex items-center gap-3">
              <button className="btn btn-primary" disabled={busy || text.trim().length < 5} onClick={submitText}>解析</button>
              <button className="btn btn-ghost" onClick={() => setText(SAMPLE)}>填入示例</button>
              {limits && <span className="text-xs text-ink-3">上限 {Math.round(limits.max_text_bytes / 1048576)} MiB / {limits.max_messages} 条</span>}
            </div>
          </div>
        </Card>
      )}

      {tab === "file" && (
        <Card>
          <div className="space-y-3">
            <Alert tone="info">
              支持：WhatsApp 官方导出 .txt、QQ 消息管理器导出 .txt、微信第三方导出 .csv/.json（行列表或 session+messages 嵌套结构）、Telegram Desktop 单聊 result.json、message-v1 JSON。
              本工具不读取、不解密任何客户端数据库。
            </Alert>
            <input type="file" accept=".txt,.csv,.json" className="input" onChange={(e) => setFiles(Array.from(e.target.files ?? []))} aria-label="选择导出文件" />
            <Options fmt={fmt} setFmt={setFmt} dateOrder={dateOrder} setDateOrder={setDateOrder} meName={meName} setMeName={setMeName} formats={settings?.formats} showDate={ambiguous || dateOrder !== ""} />
            <button className="btn btn-primary" disabled={busy || !files[0]} onClick={submitFile}>解析文件</button>
          </div>
        </Card>
      )}

      {tab === "ocr" && (
        <Card>
          <div className="space-y-3">
            {settings && !settings.ocr_available ? (
              <Alert tone="warn">
                OCR 组件未安装。在 backend 目录运行 <code className="mono">uv sync --extra ocr</code> 后重启服务。识别完全在本机进行。
              </Alert>
            ) : (
              <Alert tone="info">支持清晰的微信 / QQ 标准左右气泡截图；按对话顺序选择多张。识别在本机完成，不会上传任何图片。识别后请在校正页检查角色与时间。</Alert>
            )}
            <input type="file" accept="image/png,image/jpeg,image/webp" multiple className="input" onChange={(e) => setFiles(Array.from(e.target.files ?? []))} aria-label="选择截图" />
            {files.length > 0 && (
              <ol className="text-sm text-ink-2 list-decimal pl-5">
                {files.map((f) => <li key={f.name}>{f.name} <span className="text-ink-3">({Math.round(f.size / 1024)} KB)</span></li>)}
              </ol>
            )}
            <Field label="我的消息在截图的哪一侧？">
              <select className="input" value={meSide} onChange={(e) => setMeSide(e.target.value as "right" | "left")}>
                <option value="right">右侧（微信 / QQ 默认）</option>
                <option value="left">左侧</option>
              </select>
            </Field>
            {limits && <p className="text-xs text-ink-3">最多 {limits.max_images} 张，每张 ≤ {Math.round(limits.max_image_bytes / 1048576)} MiB。</p>}
            <button className="btn btn-primary" disabled={busy || !files.length || settings?.ocr_available === false} onClick={submitOcr}>识别截图</button>
          </div>
        </Card>
      )}

      {busy && <Spinner label="解析中…" />}
      <ErrorBox error={err} />
      {ambiguous && <Alert tone="info">请在上方“日期顺序”里选择 日/月 或 月/日，然后重试。</Alert>}
    </div>
  );
}

function Options(props: { fmt: string; setFmt: (v: string) => void; dateOrder: string; setDateOrder: (v: string) => void; meName: string; setMeName: (v: string) => void; formats?: { id: string; label: string }[]; showDate: boolean }) {
  const [open, setOpen] = useState(props.showDate);
  useEffect(() => { if (props.showDate) setOpen(true); }, [props.showDate]);
  return (
    <div>
      <button className="text-sm text-ink-3 underline" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "收起选项" : "更多选项（格式 / 日期顺序 / 我是谁）"}</button>
      {open && (
        <div className="grid gap-3 sm:grid-cols-3 mt-2">
          <Field label="格式">
            <select className="input" value={props.fmt} onChange={(e) => props.setFmt(e.target.value)}>
              <option value="auto">自动识别</option>
              {(props.formats ?? []).map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
            </select>
          </Field>
          <Field label="日期顺序（WhatsApp 有歧义时）">
            <select className="input" value={props.dateOrder} onChange={(e) => props.setDateOrder(e.target.value)}>
              <option value="">自动</option>
              <option value="DMY">日/月/年</option>
              <option value="MDY">月/日/年</option>
            </select>
          </Field>
          <Field label="我的昵称（可选）" hint="不填则第一个发言者视为“我”，之后可一键互换。">
            <input className="input" value={props.meName} onChange={(e) => props.setMeName(e.target.value)} />
          </Field>
        </div>
      )}
    </div>
  );
}
