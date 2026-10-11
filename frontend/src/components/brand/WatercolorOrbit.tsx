import { useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { Modal } from "@/components/ui/primitives";
import { ANIMALS, ART_ROOT, SIGNS, ZodiacArt, type ArtKind } from "./Brand";

function position(index: number, radius: number) {
  const angle = (index * 30 - 90) * Math.PI / 180;
  return { left: `${50 + Math.cos(angle) * radius}%`, top: `${50 + Math.sin(angle) * radius}%` };
}

export function WatercolorOrbit({ compact = false }: { compact?: boolean }) {
  const ring = useRef<HTMLDivElement>(null);
  const [paused, setPaused] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [selected, setSelected] = useState<{ kind: ArtKind; index: number } | null>(null);
  const [pure, setPure] = useState(false);
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => setReduced(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    ring.current?.getAnimations({ subtree: true }).forEach(animation => {
      if (animation instanceof CSSAnimation && animation.animationName.startsWith("atelier-orbit")) animation.updatePlaybackRate(speed);
    });
  }, [speed, reduced]);
  const label = selected ? (selected.kind === "animal" ? `生肖 · ${ANIMALS[selected.index]}` : SIGNS[selected.index]) : "";
  return (
    <div className={`watercolor-orbit ${compact ? "watercolor-orbit--compact" : ""} ${pure ? "is-pure" : ""}`}>
      <div ref={ring} className={`orbit-scene ${paused || !!selected || reduced ? "is-paused" : ""}`} aria-label="十二生肖与十二星座关系星图">
        <div className="orbit-circle orbit-circle--outer" aria-hidden="true" /><div className="orbit-circle orbit-circle--inner" aria-hidden="true" />
        <div className="orbit-center" aria-hidden="true"><span className="orbit-heart" /><small>你与我的小小宇宙</small></div>
        {!compact && <img className="orbit-figure" src={`${ART_ROOT}hero-woman-cat.png`} alt="" width="1254" height="1254" fetchPriority="high" />}
        <div className="orbit-track orbit-track--animals">{ANIMALS.map((name, index) => compact ? (
          <span key={name} className="orbit-node orbit-animal" style={position(index, 42)}><ZodiacArt kind="animal" index={index} /></span>
        ) : (
          <button key={name} type="button" className="orbit-node orbit-animal" style={position(index, 42)} aria-label={`认识生肖${name}`} onClick={() => setSelected({ kind: "animal", index })}><ZodiacArt kind="animal" index={index} /><span className="orbit-tooltip">{name}</span></button>
        ))}</div>
        <div className="orbit-track orbit-track--signs">{SIGNS.map((name, index) => compact ? (
          <span key={name} className="orbit-node orbit-sign" style={position(index + .5, 26)}><ZodiacArt kind="sign" index={index} /></span>
        ) : (
          <button key={name} type="button" className="orbit-node orbit-sign" style={position(index + .5, 26)} aria-label={`认识${name}`} onClick={() => setSelected({ kind: "sign", index })}><ZodiacArt kind="sign" index={index} /><span className="orbit-tooltip">{name}</span></button>
        ))}</div>
      </div>
      {!compact && <>
        <p className="orbit-caption">十二生肖 · 十二星座 · 万千种相遇</p>
        <div className="orbit-toolbar" aria-label="星轨控制">
          <button type="button" onClick={() => setPaused(value => !value)} aria-pressed={paused} disabled={reduced}>{paused ? <Play size={12} /> : <Pause size={12} />}{reduced ? "静态星图" : paused ? "继续旋转" : "暂停星轨"}</button>
          {!reduced && <div role="group" aria-label="旋转速度">{[[.6,"慢赏"],[1,"漫游"],[1.8,"流转"]].map(([value, text]) => <button key={value} type="button" aria-pressed={speed === value} onClick={() => setSpeed(Number(value))}>{text}</button>)}</div>}
          <button type="button" aria-pressed={pure} onClick={() => setPure(value => !value)}>{pure ? "返回画卷" : "纯净星图"}</button>
        </div>
      </>}
      <Modal open={!!selected} onClose={() => setSelected(null)} title={label} width={520}>
        {selected && <div className="medallion-detail"><ZodiacArt {...selected} /><p>{selected.kind === "animal" ? "从一个关于成长的故事，认识彼此。" : "让一点浪漫与好奇，成为对话的开始。"}</p><blockquote>{selected.kind === "animal" ? "你小时候，家人怎么讲你的生肖故事？" : "关于你的星座，哪些描述像你，哪些完全不像？"}</blockquote><div className="medallion-pager"><button type="button" className="btn btn-ghost" aria-label="上一个图案" onClick={() => setSelected({ ...selected, index: (selected.index + 11) % 12 })}>←</button><span>{selected.index + 1} / 12</span><button type="button" className="btn btn-ghost" aria-label="下一个图案" onClick={() => setSelected({ ...selected, index: (selected.index + 1) % 12 })}>→</button></div><small>文化与趣味探索，不参与好感度评分。</small></div>}
      </Modal>
    </div>
  );
}
