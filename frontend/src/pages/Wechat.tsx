import { lazy, Suspense, useState } from "react";
import { DshReader } from "@/components/DshReader";
import { Badge, Button, Spinner } from "@/components/ui";

const WechatVisualReader = lazy(() => import("@/components/WechatVisualReader").then(module => ({ default: module.WechatVisualReader })));

export function WechatPage() {
  const [source, setSource] = useState<"dsh" | "visual">("dsh");

  return <div className="space-y-5">
    <div><p className="atelier-eyebrow">CONVERSATIONS THROUGH TIME</p><h1 className="mt-2">沿着对话，慢慢读懂彼此。</h1><p className="mt-2 text-sm leading-relaxed text-ink-2">默认通过 DSH 读取本机微信数据库。指定一位好友，校对聊天记录后即可分析、归档和导出。</p></div>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Badge tone={source === "dsh" ? "good" : "warn"}>{source === "dsh" ? "默认读取方式：DSH 数据库" : "备用读取方式：微信画面"}</Badge>
      <Button size="sm" onClick={() => setSource(source === "dsh" ? "visual" : "dsh")}>{source === "dsh" ? "使用画面读取（备用）" : "返回 DSH 数据库读取"}</Button>
    </div>
    {source === "dsh" ? <DshReader /> : <Suspense fallback={<Spinner label="加载画面读取…" />}><WechatVisualReader /></Suspense>}
  </div>;
}
