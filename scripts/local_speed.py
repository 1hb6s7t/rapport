"""Local-model speed: how long does one complete analysis of an 80-message window take? (v0.8 §16)

The plan's gate is "80 条窗口完整分析 ≤ 3 分钟 on the reference GPU". This drives the exact
product path (`run_byok`: extraction → synthesis → validation → contextual intent, up to 8
targets) with the local (Ollama) profile, times every stage from the pipeline's own progress
hooks, and repeats so the first run (model load) and the warm runs are reported separately.

Nothing here touches real material: the input is the synthetic warm fixture's last 80 messages.
The report goes under `~/.rapport/eval/` like every other evaluation output.

Usage:
    uv run python ../scripts/local_speed.py --preset ollama --messages 80 --repeat 3 --out ~/.rapport/eval/local-8b-speed.md
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _local import is_local_preset, make_local_provider  # noqa: E402
from _private import ensure_private_out, private_eval_dir  # noqa: E402
from _probe import Probe  # noqa: E402

from rapport.config import PROVIDER_PRESETS, reset_settings  # noqa: E402
from rapport.jobs.pipeline import ByokContext, PipelineHooks, Progress, run_byok  # noqa: E402
from rapport.llm.extract import build_payload  # noqa: E402
from rapport.llm.limits import TASKS  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "fixtures" / "synthetic" / "paste_ab_warm.txt"
GATE_SECONDS = 180.0


def _gpu_name() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return (out.stdout or "").strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return "无独显 / nvidia-smi 不可用"


class StageClock:
    """Turns the pipeline's progress callbacks into per-stage wall-clock seconds."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: list[tuple[str, float]] = []
        self.last_stage: str | None = None

    def progress(self, p: Progress) -> None:
        if p.stage != self.last_stage:
            self.marks.append((p.stage, time.perf_counter() - self.t0))
            self.last_stage = p.stage

    def durations(self) -> dict[str, float]:
        out: dict[str, float] = {}
        end = time.perf_counter() - self.t0
        for i, (stage, at) in enumerate(self.marks):
            nxt = self.marks[i + 1][1] if i + 1 < len(self.marks) else end
            out[stage] = out.get(stage, 0.0) + (nxt - at)
        return out


def run_once(preset: str, model: str | None, events, conv, profile: Profile, timeout: float) -> dict:
    p = PROVIDER_PRESETS[preset]
    providers = {t: make_local_provider(preset, model or (p.task_models or {}).get(t), None, task=t, timeout=timeout) for t in TASKS}
    ctx = ByokContext(
        provider=providers["intent"],
        providers=providers,
        payload=build_payload(conv, events),
        versions_extra={"provider": preset, "model": providers["intent"].info.model, "price_date": p.price.verified_on},
        preset=preset,
    )
    clock = StageClock()
    hooks = PipelineHooks(progress=clock.progress)
    t0 = time.perf_counter()
    try:
        rep = run_byok(conv, events, profile, ctx, hooks)
    except LLMError as e:
        return {"error": f"{e.code}: {e.msg}", "seconds": time.perf_counter() - t0, "stages": clock.durations()}
    usage = rep.cost.actual_usage
    return {
        "error": None,
        "seconds": time.perf_counter() - t0,
        "stages": clock.durations(),
        "calls": usage.calls if usage else 0,
        "tokens_in": usage.prompt_tokens if usage else 0,
        "tokens_out": usage.completion_tokens if usage else 0,
        "ai_status": rep.ai_status,
        "intent_readings": len(rep.intent_readings),
        "total": rep.affinity.total,
        "models": sorted({pv.info.model for pv in providers.values()}),
    }


def render(preset: str, n_msgs: int, rows: list[dict], gpu: str) -> str:
    import datetime as _dt

    L = [
        "# 本地模型速度：一次完整分析（v0.8 §16）",
        "",
        f"- 日期：{_dt.date.today().isoformat()} · 机器：{gpu} · {platform.platform()}",
        f"- 预设 `{preset}` · 模型 {', '.join(rows[0].get('models') or ['—']) if rows else '—'} · 输入：合成 warm fixture 末尾 {n_msgs} 条",
        "- 口径：产品路径 `run_byok`（提取 → 综合 → 校验 → 逐句语义 ≤8 处）的墙钟秒数；第 1 轮含模型加载，之后为热身后。不是质量指标。",
        "",
        "| 轮 | 总秒 | 提取 | 综合+校验 | 语义 | 调用数 | 输入/输出 token | 语义处数 | AI 状态 | 备注 |",
        "|---:|---:|---:|---:|---:|---:|---|---:|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        st = r.get("stages", {})
        synth = st.get("synthesizing", 0.0) + st.get("validating", 0.0)
        if r["error"]:
            L.append(f"| {i} | {r['seconds']:.0f} | {st.get('extracting', 0):.0f} | {synth:.0f} | {st.get('intent', 0):.0f} | — | — | — | 失败 | {r['error']} |")
            continue
        L.append(
            f"| {i} | {r['seconds']:.0f} | {st.get('extracting', 0):.0f} | {synth:.0f} | {st.get('intent', 0):.0f} | {r['calls']} | "
            f"{r['tokens_in']}/{r['tokens_out']} | {r['intent_readings']} | {r['ai_status']} | {'含模型加载' if i == 1 else ''} |"
        )
    ok = [r["seconds"] for r in rows if not r["error"]]
    warm = ok[1:] if len(ok) > 1 else ok
    L += ["", "## 判读", ""]
    if ok:
        L.append(f"- 全部轮次 {len(ok)}/{len(rows)} 成功；最慢 {max(ok):.0f} s，热身后均值 {sum(warm) / len(warm):.0f} s（{len(warm)} 轮）。")
        L.append(f"- 门 ≤ {GATE_SECONDS:.0f} s（含首轮加载）：{'过' if max(ok) <= GATE_SECONDS else '未过'}。")
    else:
        L.append("- 没有任何一轮成功，不能判断速度。")
    L.append("- 「语义处数」少于 8 说明 select_targets 在这段材料上只挑出这么多目标，不是模型跳过。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="ollama", help="ollama | ollama-split | ollama-lite")
    ap.add_argument("--model", default=None, help="override every task's model (default: the preset's task_models)")
    ap.add_argument("--messages", type=int, default=80)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--progress", default=None)
    args = ap.parse_args()
    if not is_local_preset(args.preset):
        ap.error(f"--preset 必须是本地预设：{', '.join(k for k, p in PROVIDER_PRESETS.items() if p.native == 'ollama')}")

    probe = Probe(args.progress)
    reset_settings(data_dir=private_eval_dir() / "run-local-speed")
    conv, _parser = parse(FIXTURE.read_text(encoding="utf-8"), "warm", None, FIXTURE.name, ParseOptions())
    events = conv.events()[-args.messages :]
    profile = Profile(me=Person(alias="我"), other=Person(alias="对方"), goal="invite")
    gpu = _gpu_name()
    probe.line(f"preset={args.preset} messages={len(events)} repeat={args.repeat} gpu={gpu}")

    ping = make_local_provider(args.preset, args.model, None, task="intent", timeout=args.timeout).ping()
    probe.line(f"ping ok={ping[0]} msg={ping[1]}")
    if not ping[0]:
        return 1

    rows: list[dict] = []
    for i in range(1, args.repeat + 1):
        r = run_once(args.preset, args.model, events, conv, profile, args.timeout)
        rows.append(r)
        st = r.get("stages", {})
        if r["error"]:
            probe.line(f"  [{i}/{args.repeat}] 失败 {r['error']} · {r['seconds']:.0f}s")
        else:
            probe.line(
                f"  [{i}/{args.repeat}] {r['seconds']:.0f}s · 提取 {st.get('extracting', 0):.0f}s 综合 {st.get('synthesizing', 0) + st.get('validating', 0):.0f}s "
                f"语义 {st.get('intent', 0):.0f}s · {r['calls']} 次调用 · 语义 {r['intent_readings']} 处 · ai={r['ai_status']} total={r['total']}"
            )
        probe.done(f"DONE rep={i} seconds={r['seconds']:.1f} error={r['error']}")

    out = ensure_private_out(args.out, f"local-speed-{args.preset}.md")
    out.write_text(render(args.preset, len(events), rows, gpu), encoding="utf-8")
    probe.line(f"\n报告写入 {out}")
    return 0 if any(not r["error"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
