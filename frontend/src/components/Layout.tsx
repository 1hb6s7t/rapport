/** 应用外壳：左侧导航栏 + 顶部上下文栏 + 主工作区（docs/DESIGN.md §6–7）。
 *
 *  侧栏固定 232px，暖白底；激活项用 3px 玫红指示条 + 极浅粉底，不做实色块。
 *  顶部栏承载：页面名、当前对象、提醒铃铛与本机连接状态。
 *  小于 lg 时侧栏收成抽屉；主工作区永远可到达。 */

import { Suspense, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Bell,
  BookHeart,
  Clock3,
  FileText,
  FileUp,
  HeartHandshake,
  Home,
  ListChecks,
  MessageSquareReply,
  Menu,
  ScanText,
  Sprout,
  Settings2,
  Target,
} from "lucide-react";
import { api, getCsrf } from "@/api/client";
import { Modal, Spinner } from "@/components/ui";
import { BrandFilters, BrandLogo } from "@/components/brand/Brand";
import { PetalEffects } from "@/components/brand/PetalEffects";
import { useRelationships } from "@/components/RelationshipPicker";
import { useFlow } from "@/store";
import type { InboxView } from "@/api/types";

export function useHealth() {
  const [health, setHealth] = useState<{ ok: boolean; version: string; paired: boolean; ocr_available: boolean; desktop_available?: boolean } | null>(null);
  useEffect(() => {
    api.get<typeof health>("/health").then(setHealth).catch(() => setHealth(null));
  }, []);
  return health;
}

function useUnread(paired: boolean) {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!paired) return;
    let stop = false;
    const pull = () => api.get<InboxView>("/drive/inbox?unread=true&limit=1").then((v) => { if (!stop) setN(v.unread); }).catch(() => {});
    pull();
    const t = window.setInterval(pull, 30000);
    return () => { stop = true; window.clearInterval(t); };
  }, [paired]);
  return n;
}

const NAV: { group: string; items: { to: string; label: string; icon: ReactNode }[] }[] = [
  {
    group: "流程",
    items: [
      { to: "/", label: "首页", icon: <Home size={17} strokeWidth={1.7} /> },
      { to: "/import", label: "导入对话", icon: <FileUp size={17} strokeWidth={1.7} /> },
      { to: "/wechat", label: "微信聊天读取", icon: <ScanText size={17} strokeWidth={1.7} /> },
      { to: "/review", label: "校正", icon: <ListChecks size={17} strokeWidth={1.7} /> },
      { to: "/profile", label: "标签与目标", icon: <Target size={17} strokeWidth={1.7} /> },
      { to: "/report", label: "分析报告", icon: <FileText size={17} strokeWidth={1.7} /> },
    ],
  },
  {
    group: "深入",
    items: [
      { to: "/diary", label: "日记", icon: <BookHeart size={17} strokeWidth={1.7} /> },
      { to: "/replies", label: "代回复", icon: <MessageSquareReply size={17} strokeWidth={1.7} /> },
      { to: "/workbench", label: "个人学习工作台", icon: <Sprout size={17} strokeWidth={1.7} /> },
      { to: "/history", label: "历史", icon: <Clock3 size={17} strokeWidth={1.7} /> },
    ],
  },
];

const PAGE_TITLES: [RegExp, string][] = [
  [/^\/$/, "首页"],
  [/^\/import/, "导入对话"],
  [/^\/wechat/, "微信聊天读取"],
  [/^\/review/, "校正"],
  [/^\/profile/, "标签与目标"],
  [/^\/preview/, "出网预览"],
  [/^\/progress/, "分析进行中"],
  [/^\/report/, "分析报告"],
  [/^\/diary/, "日记"],
  [/^\/replies/, "代回复"],
  [/^\/workbench/, "个人学习工作台"],
  [/^\/history/, "历史"],
  [/^\/inbox/, "提醒"],
  [/^\/settings/, "设置与数据"],
];

function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex-1 overflow-y-auto px-3 pb-4" aria-label="主导航">
      {NAV.map((g) => (
        <div key={g.group} className="mt-4 first:mt-2">
          <p className="px-2.5 pb-1 text-[11px] tracking-wide text-ink-3">{g.group}</p>
          <ul className="space-y-0.5">
            {g.items.map((it) => (
              <li key={it.to}>
                <NavLink
                  to={it.to}
                  end={it.to === "/"}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    `group relative flex items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-sm no-underline transition-colors duration-150 ${
                      isActive ? "bg-brand-soft text-brand-ink font-medium" : "text-ink-2 hover:bg-brand-mist hover:text-ink"
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      <span
                        aria-hidden
                        className={`absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full transition-opacity ${isActive ? "bg-brand-strong opacity-100" : "opacity-0"}`}
                      />
                      <span className={isActive ? "text-brand-ink" : "text-ink-3 group-hover:text-ink-2"}>{it.icon}</span>
                      {it.label}
                    </>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function SidebarFooter() {
  return (
    <div className="border-t border-line px-4 py-3">
      <NavLink
        to="/settings"
        className={({ isActive }) =>
          `flex items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-sm no-underline transition-colors ${isActive ? "bg-brand-soft text-brand-ink font-medium" : "text-ink-2 hover:bg-brand-mist"}`
        }
      >
        <Settings2 size={17} strokeWidth={1.7} className="text-ink-3" />
        设置与数据
      </NavLink>
      <div className="mt-2.5 flex items-center gap-1.5 px-2.5 text-[11px] text-ink-3">
        <span className="h-1.5 w-1.5 rounded-full bg-mint-deep" aria-hidden />
        本地运行 · 默认零外发
      </div>
    </div>
  );
}

export function Layout() {
  const location = useLocation();
  const paired = !!getCsrf();
  const unread = useUnread(paired);
  const [drawer, setDrawer] = useState(false);
  const [flow, setFlow] = useFlow();
  const [rels] = useRelationships();

  useEffect(() => setDrawer(false), [location.pathname]);

  const title = PAGE_TITLES.find(([re]) => re.test(location.pathname))?.[1] ?? "心迹";
  const sidebar = (
    <div className="flex h-full flex-col">
      <Link to="/" className="flex items-center gap-2.5 px-4 pt-5 pb-2 no-underline" onClick={() => setDrawer(false)}>
        <BrandLogo />
      </Link>
      <SidebarNav onNavigate={() => setDrawer(false)} />
      <SidebarFooter />
    </div>
  );

  return (
    <div className="workspace-shell min-h-screen">
      <BrandFilters />
      <PetalEffects quiet />
      <a className="atelier-skip-link" href="#workspace-main">跳到工作区</a>
      {/* 桌面侧栏 */}
      <aside className="atelier-sidebar fixed inset-y-0 left-0 z-30 hidden w-[232px] border-r border-line bg-card/70 backdrop-blur lg:block" aria-label="侧边导航">
        {sidebar}
      </aside>

      {/* 移动端抽屉 */}
      <Modal open={drawer} onClose={() => setDrawer(false)} title="页面导航" width={300} placement="left">{sidebar}</Modal>

      <div className="flex min-h-screen flex-col lg:pl-[232px]">
        {/* 顶部上下文栏 */}
        <header className="workspace-contextbar sticky top-0 z-20 border-b border-line bg-paper/80 backdrop-blur">
          <div className="flex items-center gap-3 px-4 py-2.5 lg:px-6">
            <button className="btn btn-ghost p-1.5 lg:hidden" onClick={() => setDrawer(true)} aria-label="打开导航">
              <Menu size={18} strokeWidth={1.7} />
            </button>
            <div className="flex min-w-0 items-baseline gap-2">
              <h1 className="text-[15px] font-semibold truncate">{title}</h1>
              <span className="hidden md:block text-xs text-ink-3 truncate">基于真实聊天证据，而不是猜测</span>
            </div>
            <div className="ml-auto flex items-center gap-2">
              {paired && rels && rels.length > 0 && (
                <label className="hidden sm:flex items-center gap-1.5 text-xs text-ink-3">
                  <HeartHandshake size={14} strokeWidth={1.7} className="text-ink-3" />
                  <select
                    className="input !w-auto !py-1 !text-xs"
                    value={flow.relationshipId}
                    onChange={(e) => setFlow({ relationshipId: e.target.value, importId: null, analysisId: null, reportId: null })}
                    aria-label="当前对象"
                  >
                    {rels.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <NavLink to="/inbox" aria-label={`提醒收件箱，${unread} 条未读`} className="btn btn-ghost relative p-2">
                <Bell size={17} strokeWidth={1.7} />
                {unread > 0 && (
                  <span className="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 rounded-full bg-brand-strong text-white text-[10px] leading-4 text-center">{unread > 9 ? "9+" : unread}</span>
                )}
              </NavLink>
              <span className="badge">本机已连接</span>
            </div>
          </div>
        </header>

        {/* 主工作区 */}
        <main id="workspace-main" className="workspace-main flex-1 px-4 py-6 lg:px-8 lg:py-7">
          <div className="mx-auto w-full max-w-[1120px]">
            <Suspense fallback={<div className="card py-10"><Spinner label="打开页面…" /></div>}><Outlet /></Suspense>
          </div>
        </main>

        <footer className="workspace-footer border-t border-line px-4 py-3.5 text-center text-xs text-ink-3">
          本地运行 · 默认零外发 · 好感度指数不是概率 · 不构成心理评估 · MIT 开源
        </footer>
      </div>
    </div>
  );
}
