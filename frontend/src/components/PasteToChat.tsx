import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { Badge } from "@/components/ui";

interface DesktopConfig { available: boolean; enabled: boolean; allow_send: boolean; note: string }
interface PasteResult { ok: boolean; reason: string; window_title: string; control_type: string; sent: boolean }

const REASON: Record<string, string> = {
  pasted: "已粘贴",
  ready: "可以粘贴",
  unavailable: "本机未安装桌面扩展",
  no_focus: "没有聚焦的输入框",
  focus_is_this_app: "焦点还在心迹自己的窗口里",
  no_editable_focus: "当前焦点不是可输入的文本框",
  empty_text: "草稿为空",
  paste_disabled: "功能未开启",
  send_not_allowed: "未允许直接发送",
};

/** 「粘贴到聊天窗口」：倒计时 3 秒，期间用户点进微信/QQ 的输入框；到点后经 Windows 无障碍接口把
 * 草稿粘进去。默认不回车；「发送」只在设置里允许后出现，且每条都要单独点。 */
export function PasteToChat({ text, source = "reply" }: { text: string; source?: string }) {
  const [cfg, setCfg] = useState<DesktopConfig | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [res, setRes] = useState<PasteResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<DesktopConfig>("/desktop/config").then(setCfg).catch(() => setCfg(null));
  }, []);

  if (!cfg || !cfg.available || !cfg.enabled) return null;

  const go = async (send: boolean) => {
    setRes(null);
    setBusy(true);
    for (let s = 3; s > 0; s--) {
      setCount(s);
      await new Promise((r) => setTimeout(r, 1000));
    }
    setCount(null);
    try {
      setRes(await api.post<PasteResult>("/desktop/paste", { text, send, delay_s: 0, source }));
    } catch (e) {
      setRes({ ok: false, reason: (e as { code?: string })?.code ?? "error", window_title: "", control_type: "", sent: false });
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button className="btn btn-ghost py-0.5 text-xs" disabled={busy} onClick={() => go(false)} title="点击后 3 秒内把光标放进聊天软件的输入框">
        {count !== null ? `${count} 秒后粘贴…请点进聊天输入框` : "粘贴到聊天窗口"}
      </button>
      {cfg.allow_send && (
        <button className="btn btn-danger py-0.5 text-xs" disabled={busy} onClick={() => go(true)} title="粘贴并按回车。每条都需要你点这一下。">
          {count !== null ? "…" : "粘贴并发送"}
        </button>
      )}
      {res && (
        <Badge tone={res.ok ? "good" : "warn"}>
          {REASON[res.reason] ?? res.reason}
          {res.ok && res.window_title ? ` → ${res.window_title.slice(0, 18)}` : ""}
          {res.sent ? "（已回车）" : ""}
        </Badge>
      )}
    </span>
  );
}
