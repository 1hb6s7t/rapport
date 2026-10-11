import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { LocalDoctor, LocalPullStatus } from "@/api/types";
import { Alert, Badge, Card, Field } from "@/components/ui";

const PROFILE_ORDER = ["solo", "split", "lite"] as const;
type Profile = (typeof PROFILE_ORDER)[number];
export const TASK_LABELS: Record<string, string> = { extract: "提取", synthesize: "综合", intent: "语义", reply: "代回复" };

function gb(n: number) {
  return (n / 1e9).toFixed(1);
}

/** 「一键本地」：检测 Ollama 与显存、选档位、拉权重、切到本地模式。除拉权重外全程零出网。 */
export function LocalModelsCard({ currentPreset, onSwitched }: { currentPreset: string; onSwitched: () => Promise<void> | void }) {
  const [doc, setDoc] = useState<LocalDoctor | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile>("solo");
  const [pull, setPull] = useState<LocalPullStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  async function refresh() {
    try {
      const d = await api.get<LocalDoctor>("/models/doctor");
      setDoc(d);
      setErr(null);
      return d;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  function stopPolling() {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
  }

  function startPolling() {
    stopPolling();
    timer.current = window.setInterval(async () => {
      const p = await api.get<LocalPullStatus>("/models/pull").catch(() => null);
      if (!p) return;
      setPull(p);
      if (!p.running) {
        stopPolling();
        await refresh();
      }
    }, 1500);
  }

  useEffect(() => {
    refresh().then((d) => {
      if (d && (PROFILE_ORDER as readonly string[]).includes(d.recommended_profile)) setProfile(d.recommended_profile as Profile);
    });
    api.get<LocalPullStatus>("/models/pull").then((p) => { setPull(p); if (p.running) startPolling(); }).catch(() => null);
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function startPull() {
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/models/pull", { profile });
      startPolling();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function switchLocal() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.post<{ ready: boolean; preset: string }>("/models/local-on", { profile });
      setMsg(r.ready ? `已切到本地 ${profile} 档，连接模式=本地。分析、语义、代回复全部在本机运行。` : `已切到本地 ${profile} 档，但模型尚未就绪：请先拉取权重。`);
      await onSwitched();
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const p = doc?.profiles[profile];
  const taskModels = doc?.presets_task_models?.[profile];
  const isLocalNow = currentPreset.startsWith("ollama") || currentPreset === "style-local";

  return (
    <Card title="本地模型（零出网）" right={isLocalNow ? <Badge tone="good">当前：本地</Badge> : <Badge>当前：云端预设</Badge>}>
      <div className="space-y-3 text-sm">
        <p className="text-xs text-ink-3">
          聊天记录不出这台电脑：模型权重在本机由 Ollama 运行，不需要任何 API Key。四类任务（提取 / 综合 / 语义 / 代回复）可以由一个模型包办，也可以分工。
        </p>
        {err && <Alert tone="warn">读取本地模型状态失败：{err}</Alert>}
        {doc && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-ink-3">Ollama</dt>
            <dd>
              {doc.ollama_installed
                ? doc.ollama_running
                  ? <span className="text-good">运行中 · v{doc.ollama_version}</span>
                  : <span className="text-warn">已安装但未运行（打开 Ollama 应用，或运行 <span className="mono">ollama serve</span>）</span>
                : <span className="text-warn">未安装 · Windows 运行 <span className="mono">winget install Ollama.Ollama</span>，或访问 ollama.com/download</span>}
            </dd>
            <dt className="text-ink-3">硬件</dt>
            <dd>{doc.gpu ? `${doc.gpu} · 显存 ${doc.vram_gb} GB` : "未检测到 NVIDIA 独显（将走 CPU）"}{doc.ram_gb ? ` · 内存 ${doc.ram_gb} GB` : ""}</dd>
            <dt className="text-ink-3">已拉取</dt>
            <dd>{doc.models.length ? doc.models.map((m) => `${m.name}（${m.size_gb} GB）`).join("，") : "无"}{doc.models_dir && <span className="block text-ink-3">目录 <span className="mono">{doc.models_dir}</span>（改目录：设置环境变量 OLLAMA_MODELS 后重启 Ollama）</span>}</dd>
          </dl>
        )}
        <Field label="档位" hint={doc ? `按本机硬件建议：${doc.recommended_profile}` : undefined}>
          <div className="space-y-1">
            {PROFILE_ORDER.map((k) => {
              const info = doc?.profiles[k];
              return (
                <label key={k} className="flex items-start gap-2">
                  <input type="radio" name="local-profile" className="mt-1" checked={profile === k} onChange={() => setProfile(k)} />
                  <span>
                    <span className="font-medium">{k}</span>{info ? ` · ${info.label}` : ""}
                    {info && (
                      <span className="block text-xs text-ink-3">
                        {info.ready ? <span className="text-good">就绪</span> : info.missing.length ? `缺 ${info.missing.join("、")}` : "Ollama 未运行"}
                      </span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        </Field>
        {p && taskModels && (
          <div className="text-xs text-ink-3">
            分工：{Object.entries(TASK_LABELS).map(([t, l]) => <span key={t} className="mr-3">{l} → <span className="mono">{taskModels[t] ?? p.models[0]}</span></span>)}
          </div>
        )}
        {pull && pull.items.length > 0 && (
          <div className="space-y-1">
            {pull.items.map((it) => (
              <div key={it.model} className="text-xs">
                <div className="flex justify-between"><span className="mono">{it.model}</span><span>{it.done ? "完成" : it.total ? `${gb(it.completed)} / ${gb(it.total)} GB` : it.status || "排队"}</span></div>
                <div className="h-1.5 bg-paper rounded overflow-hidden"><div className="h-full bg-brand transition-all" style={{ width: `${it.done ? 100 : it.total ? Math.round((it.completed / it.total) * 100) : 0}%` }} /></div>
              </div>
            ))}
            {pull.error && <Alert tone="warn">拉取失败：{pull.error}</Alert>}
          </div>
        )}
        <div className="flex flex-wrap gap-2 items-center">
          <button className="btn btn-ghost" disabled={busy || !doc?.ollama_running || pull?.running === true} onClick={startPull}>{pull?.running ? "拉取中…" : "拉取权重"}</button>
          <button className="btn btn-primary" disabled={busy} onClick={switchLocal}>切到本地 · {profile}</button>
          <button className="btn btn-ghost" disabled={busy} onClick={() => refresh()}>重新检测</button>
        </div>
        {msg && <Alert tone={msg.includes("失败") || msg.includes("尚未") ? "warn" : "good"}>{msg}</Alert>}
        <div className="text-xs text-ink-3">
          {doc?.notes?.map((n) => <span key={n} className="block">· {n}</span>)}
          <span className="block">· 命令行等价：<span className="mono">rapport models doctor</span> / <span className="mono">rapport models pull --profile {profile}</span> / <span className="mono">rapport models local-on --profile {profile}</span></span>
        </div>
      </div>
    </Card>
  );
}
