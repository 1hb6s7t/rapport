import { useEffect, useRef, useState } from "react";
import { ART_ROOT } from "./Brand";

interface Petal { x: number; y: number; age: number; life: number; size: number; variant: number; angle: number; spin: number; vx: number; vy: number }

/** One listener and one idle-stopping animation loop. No chat content or network tracking. */
export function PetalEffects({ quiet = false }: { quiet?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [enabled, setEnabled] = useState(true);
  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context || !enabled) return;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    const fine = matchMedia("(hover: hover) and (pointer: fine)");
    const atlas = new Image();
    atlas.src = `${ART_ROOT}peach-petals.png`;
    let particles: Petal[] = [], frame = 0, lastFrame = 0, lastEmission = 0, lastX = 0, lastY = 0;
    let width = innerWidth, height = innerHeight;
    const allowed = () => !reduced.matches && fine.matches && innerWidth > 760 && !document.hidden;
    function clear() {
      particles = []; cancelAnimationFrame(frame); frame = 0;
      context!.clearRect(0, 0, width, height);
    }
    function resize() {
      width = innerWidth; height = innerHeight;
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas!.width = Math.round(width * ratio); canvas!.height = Math.round(height * ratio);
      context!.setTransform(ratio, 0, 0, ratio, 0, 0);
    }
    function draw(time: number) {
      const delta = Math.min((time - lastFrame) / 1000 || .016, .04); lastFrame = time;
      context!.clearRect(0, 0, width, height);
      particles = particles.filter(p => p.age < p.life);
      for (const p of particles) {
        p.age += delta; p.x += (p.vx + Math.sin(p.age * 3 + p.angle) * 7) * delta; p.y += p.vy * delta; p.vy += 7 * delta;
        context!.save(); context!.translate(p.x, p.y); context!.rotate(p.angle + p.age * p.spin);
        context!.scale(1, .82 + Math.cos(p.age * 3 + p.angle) * .18);
        context!.globalAlpha = Math.pow(Math.max(0, 1 - p.age / p.life), .65) * Math.min(1, p.age / .08) * (quiet ? .48 : .84);
        context!.drawImage(atlas, p.variant % 2 * atlas.naturalWidth / 2, Math.floor(p.variant / 2) * atlas.naturalHeight / 2, atlas.naturalWidth / 2, atlas.naturalHeight / 2, -p.size / 2, -p.size / 2, p.size, p.size);
        context!.restore();
      }
      if (particles.length && allowed()) frame = requestAnimationFrame(draw); else clear();
    }
    function emit(x: number, y: number, burst = false) {
      if (!allowed() || !atlas.complete || !atlas.naturalWidth || document.querySelector("dialog[open]")) return;
      particles.push({ x, y, age: 0, life: 1.8 + Math.random() * .7, size: (quiet ? 27 : 38) + Math.random() * 18, variant: Math.floor(Math.random() * 4), angle: Math.random() * Math.PI * 2, spin: (Math.random() - .5) * 1.5, vx: (Math.random() - .5) * (burst ? 90 : 28), vy: burst ? -15 - Math.random() * 20 : 16 + Math.random() * 15 });
      if (particles.length > (quiet ? 25 : 60)) particles.shift();
      if (!frame) { lastFrame = performance.now(); frame = requestAnimationFrame(draw); }
    }
    function move(event: PointerEvent) {
      if (!allowed() || event.pointerType === "touch" || (event.target as Element)?.closest("input,textarea,select,[contenteditable=true]")) return;
      const now = performance.now();
      if (now - lastEmission > (quiet ? 100 : 40) && Math.hypot(event.clientX - lastX, event.clientY - lastY) > 9) {
        emit(event.clientX, event.clientY); lastEmission = now; lastX = event.clientX; lastY = event.clientY;
      }
    }
    function click(event: PointerEvent) {
      if (event.pointerType !== "touch" && (event.target as Element)?.closest("button,a,summary")) for (let i = 0; i < (quiet ? 3 : 6); i++) emit(event.clientX, event.clientY, true);
    }
    function visibility() { if (document.hidden) clear(); }
    resize();
    window.addEventListener("resize", resize, { passive: true });
    document.addEventListener("pointermove", move, { passive: true });
    document.addEventListener("pointerdown", click, { passive: true });
    document.addEventListener("visibilitychange", visibility);
    reduced.addEventListener("change", clear); fine.addEventListener("change", clear);
    return () => {
      clear(); window.removeEventListener("resize", resize); document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerdown", click); document.removeEventListener("visibilitychange", visibility);
      reduced.removeEventListener("change", clear); fine.removeEventListener("change", clear);
    };
  }, [enabled, quiet]);
  return <><canvas ref={canvasRef} className="atelier-petal-canvas" aria-hidden="true" /><button type="button" className={`atelier-effects-toggle ${quiet ? "is-quiet" : ""}`} aria-pressed={enabled} aria-label={enabled ? "关闭桃花瓣效果" : "开启桃花瓣效果"} onClick={() => setEnabled(value => !value)}>{enabled ? "桃花瓣 · 已开启" : "开启桃花瓣"}</button></>;
}
