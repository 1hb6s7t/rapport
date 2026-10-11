import { useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { BookOpenCheck, ClipboardCheck, FolderHeart, Sprout, Timer } from "lucide-react";
import { WorkbenchDataset } from "@/components/workbenchDataset";
import { WorkbenchArchives } from "@/components/workbenchArchives";
import { WorkbenchGold } from "@/components/workbenchGold";
import { WorkbenchLearning, WorkbenchSchedule } from "@/components/workbenchLearning";

const SECTIONS = [
  { id: "data", label: "数据与复核", icon: BookOpenCheck },
  { id: "gold", label: "人工金标", icon: ClipboardCheck },
  { id: "archives", label: "长期档案", icon: FolderHeart },
  { id: "learning", label: "训练与版本", icon: Sprout },
  { id: "schedule", label: "自动更新", icon: Timer },
];

export function WorkbenchPage() {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const active = SECTIONS.some(section => section.id === requested) ? requested! : "data";
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  function select(id: string) { setParams({ tab: id }, { replace: true }); }
  function move(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % SECTIONS.length;
    else if (event.key === "ArrowLeft") next = (index + SECTIONS.length - 1) % SECTIONS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = SECTIONS.length - 1;
    else return;
    event.preventDefault();
    select(SECTIONS[next].id);
    tabs.current[next]?.focus();
  }
  return <div className="space-y-6">
    <header className="relative overflow-hidden rounded-[28px] border border-line bg-card/70 px-6 py-7 sm:px-8">
      <span aria-hidden className="pointer-events-none absolute -right-8 -top-16 h-64 w-64 rounded-full bg-brand-soft/60 blur-3xl" />
      <p className="relative text-xs tracking-[.25em] text-brand-ink">心迹 · 一点点，更懂你</p>
      <h2 className="relative mt-3 font-serif text-2xl text-ink sm:text-3xl">把相处的细节，留给下一次更好的回应。</h2>
      <p className="relative mt-3 max-w-2xl text-sm leading-relaxed text-ink-3">从已有记录整理证据，让模型学习你的表达。每次更新都有来处、有比较，也由你决定是否启用。</p>
    </header>
    <div role="tablist" aria-label="个人学习工作台" className="grid grid-cols-2 gap-2 rounded-2xl border border-line bg-paper/80 p-2 sm:grid-cols-5">
      {SECTIONS.map((section, index) => <button key={section.id} type="button" role="tab" id={`workbench-tab-${section.id}`} aria-controls={`workbench-panel-${section.id}`} aria-selected={active === section.id} tabIndex={active === section.id ? 0 : -1} ref={element => { tabs.current[index] = element; }} onClick={() => select(section.id)} onKeyDown={event => move(event, index)} className={`flex items-center justify-center gap-2 rounded-xl px-3 py-3 text-sm transition-colors ${active === section.id ? "bg-card font-medium text-brand-ink shadow-sm" : "text-ink-3 hover:bg-brand-mist hover:text-ink"}`}><section.icon size={17} />{section.label}</button>)}
    </div>
    <div key={active} id={`workbench-panel-${active}`} role="tabpanel" aria-labelledby={`workbench-tab-${active}`}>
      {active === "data" && <WorkbenchDataset />}
      {active === "gold" && <WorkbenchGold />}
      {active === "archives" && <WorkbenchArchives />}
      {active === "learning" && <WorkbenchLearning />}
      {active === "schedule" && <WorkbenchSchedule />}
    </div>
  </div>;
}
