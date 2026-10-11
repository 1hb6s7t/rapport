import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { ArrowUpRight, FileUp, MessageCircle, ShieldCheck, Sparkles } from "lucide-react";
import { Modal } from "@/components/ui/primitives";
import { ART_ROOT, BrandLogo, ZodiacArt } from "./Brand";
import { WatercolorOrbit } from "./WatercolorOrbit";

export function BrandLanding({ pairing }: { pairing: ReactNode }) {
  const [pairOpen, setPairOpen] = useState(false);
  const [topic, setTopic] = useState(0);
  const location = useLocation();
  const headerRef = useRef<HTMLElement>(null);
  const heroRef = useRef<HTMLElement>(null);
  useEffect(() => { if (location.hash === "#pair") setPairOpen(true); }, [location.hash]);
  useEffect(() => {
    const header = headerRef.current, hero = heroRef.current;
    if (!header || !hero) return;
    let previous = scrollY, travel = 0, frame = 0;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    function update() {
      const delta = scrollY - previous;
      travel = delta > 0 ? Math.max(0, travel) + delta : delta < 0 ? Math.min(0, travel) + delta : travel;
      header!.classList.toggle("is-scrolled", scrollY > 70);
      if (scrollY < 100 || header!.querySelector(":focus-visible")) header!.classList.remove("is-hidden");
      else if (travel > 24) header!.classList.add("is-hidden");
      else if (travel < -16) header!.classList.remove("is-hidden");
      previous = scrollY; frame = 0;
    }
    function scroll() { if (!frame) frame = requestAnimationFrame(update); }
    function focus() { header!.classList.remove("is-hidden"); }
    function move(event: PointerEvent) {
      if (reduced.matches || innerWidth < 900 || event.pointerType !== "mouse") return;
      const box = hero!.getBoundingClientRect();
      hero!.style.setProperty("--orbit-shift", `${((event.clientX - box.left) / box.width - .5) * 8}px`);
    }
    function leave() { hero!.style.setProperty("--orbit-shift", "0px"); }
    window.addEventListener("scroll", scroll, { passive: true }); header.addEventListener("focusin", focus);
    hero.addEventListener("pointermove", move, { passive: true }); hero.addEventListener("pointerleave", leave);
    update();
    return () => { cancelAnimationFrame(frame); window.removeEventListener("scroll", scroll); header.removeEventListener("focusin", focus); hero.removeEventListener("pointermove", move); hero.removeEventListener("pointerleave", leave); };
  }, []);
  const questions = ["最近有没有一个很小的瞬间，让你觉得今天还不错？", "什么样的陪伴，会让你觉得被认真理解？", "如果明天可以放一天假，你最想怎么过？", "有没有一家小店，你会想带我一起去？"];
  return <div className="brand-landing">
    <a className="atelier-skip-link" href="#landing-main">跳到主要内容</a>
    <header ref={headerRef} className="landing-header landing-wrap">
      <Link to="/" aria-label="心迹 Rapport 首页"><BrandLogo /></Link>
      <nav aria-label="首页导航"><a href="#features">认识心迹</a><a href="#understanding">理解的方式</a><Link to="/report/demo">演示报告</Link></nav>
      <button type="button" className="btn btn-primary" onClick={() => setPairOpen(true)}>开始探索 <ArrowUpRight size={14} /></button>
    </header>
    <main id="landing-main">
      <section ref={heroRef} className="landing-hero landing-wrap">
        <div className="landing-hero-copy"><p className="atelier-eyebrow">从对话，看见更真实的关系</p><h1>心迹，<br />让每一次对话<br />都有<span>回响。</span></h1><p className="landing-description">那些欲言又止，和藏在日常里的在意。<br />从对话、人格与星轨出发，<br />看见情绪，看见差异，也看见可能。</p><div className="landing-actions"><button type="button" className="btn btn-primary" onClick={() => setPairOpen(true)}>开启关系探索 <ArrowUpRight size={16} /></button><Link to="/report/demo" className="landing-demo"><Sparkles size={15} /> 看一段心迹</Link></div><p className="landing-footnote"><ShieldCheck size={14} /> 本地优先 · 以对话为据，把决定留给你</p></div>
        <div className="landing-orbit"><WatercolorOrbit /></div>
        <p className="landing-poem" aria-hidden="true">好的关系，<br />从理解开始。</p>
      </section>
      <section className="landing-features landing-wrap" id="features"><div className="landing-section-heading"><p className="atelier-eyebrow">A LITTLE CURIOSITY, A LITTLE CLOSER</p><h2>理解一个人，不止一种视角。</h2><p>从一句话、一种性格，到一个关于相遇的故事。</p></div><div className="landing-feature-grid">
        <Link className="landing-feature" to="/report/demo"><img src={`${ART_ROOT}conversation-watercolor.png`} alt="水彩对话与白猫" width="154" height="154" loading="lazy" /><h3>对话里的微光</h3><p>回到真实聊天，<br />发现每一个有依据的细节。</p><span>查看演示报告 <ArrowUpRight size={13} /></span></Link>
        <button className="landing-feature" type="button" onClick={() => { setTopic(1); document.querySelector("#understanding")?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); }}><div className="atelier-flower" aria-hidden="true"><span>I</span><span>N</span><span>F</span><span>P</span></div><h3>各自独特的频率</h3><p>把人格当作起点，<br />让真实的交流修正最初的印象。</p><span>看看沟通的另一面 <ArrowUpRight size={13} /></span></button>
        <button className="landing-feature" type="button" onClick={() => setPairOpen(true)}><ZodiacArt kind="sign" index={2} /><h3>同一片温柔星空</h3><p>给好奇一个浪漫入口，<br />为真实的你保留更多可能。</p><span>开始认识彼此 <ArrowUpRight size={13} /></span></button>
        <button className="landing-feature" type="button" onClick={() => setPairOpen(true)}><ZodiacArt kind="animal" index={3} /><h3>时光留下的印记</h3><p>从小听过的故事，<br />藏着各不相同的成长轨迹。</p><span>开启关系档案 <ArrowUpRight size={13} /></span></button>
      </div></section>
      <section className="landing-landscape"><div className="landing-wrap"><div><p className="atelier-eyebrow">A JOURNEY TOWARDS EACH OTHER</p><h2>在对话的流动里，<br />遇见更好的彼此。</h2><p className="landscape-sign">R A P P O R T</p><button type="button" className="btn btn-primary" onClick={() => setPairOpen(true)}>开启你的关系地图 <ArrowUpRight size={15} /></button><small>让理解，成为更靠近的开始。</small></div></div></section>
      <section className="landing-understanding landing-wrap" id="understanding"><div><p className="atelier-eyebrow">GENTLE HEART, CLEAR EYES</p><h2>星星描绘浪漫，<br />对话让理解发生。</h2><p>每一个观察，都应该有据可循。<br />看看具体说了什么，也保留另一种解释。<br />把选择留给自己，把理解留给彼此。</p><Link className="btn btn-soft" to="/report/demo">体验一份完整报告 <ArrowUpRight size={14} /></Link><div className="landing-values"><span><ShieldCheck size={14} /> 本机存储</span><span><MessageCircle size={14} /> 证据可追溯</span><span><FileUp size={14} /> 发送前确认</span></div></div><div className="landing-question"><span className="atelier-eyebrow">从一个好问题开始</span><blockquote aria-live="polite">“{questions[topic]}”</blockquote><button type="button" className="btn btn-ghost" onClick={() => setTopic(value => (value + 1) % questions.length)}>换一个话题 ↻</button><small>标签不参与好感度评分。相处中的真实回应，始终更重要。</small></div></section>
    </main>
    <footer className="landing-footer landing-wrap"><Link to="/" aria-label="回到心迹首页"><BrandLogo /></Link><p>让理解发生，让关系自然生长。</p><Link to="/report/demo">查看演示报告 ↗</Link></footer>
    <Modal open={pairOpen} onClose={() => setPairOpen(false)} title="连接你的本地工作区" width={460}><div className="landing-pairing">{pairing}</div></Modal>
  </div>;
}
