"""Affinity stability: is the number the same when nothing that matters changed?

Two questions, two modes:

- `--mode rules` (free, no model calls): the rule layer must be deterministic (same material twice
  ⇒ byte-identical total and dims) and insensitive to which tenth of the conversation happens to
  be in the file. It resamples by dropping a share of the messages `--resample` times and reports
  the spread. Plan §1's gate: spread ≤5 points.
- `--mode byok` (paid): the same fixture through the model `--repeat` times, reporting the spread
  of the fused total. A number that moves 15 points between two identical runs is not a number.

The synthetic fixtures are in `fixtures/synthetic/`. Real material must not be passed here; the
only script that takes real chats is `private_rating_sheet.py`, which writes under `~/.rapport/eval/`.

Usage:
    uv run python ../scripts/affinity_stability.py --mode rules --resample 20
    uv run python ../scripts/affinity_stability.py "C:/path/API KEY.txt" --mode byok --repeat 3 --out ~/.rapport/eval/aff-stability.md
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, private_eval_dir  # noqa: E402
from _probe import Probe  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import PROVIDER_PRESETS, reset_settings  # noqa: E402
from rapport.jobs.pipeline import ByokContext, run_byok, run_rules  # noqa: E402
from rapport.llm.extract import build_payload  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402
from rapport.schemas.report_v1 import DIMENSIONS  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC = REPO / "fixtures" / "synthetic"
FIXTURES = {
    "warm": "paste_ab_warm.txt",
    "cooling": "paste_ab_cooling.txt",
    "stop": "paste_ab_stop_contact.txt",
    # Plan §1's gates name warm and cooling explicitly; stop is the boundary case.
}
# Plan §1 acceptance thresholds, applied to the rules layer.
WARM_MIN = 65
COOLING_MAX = 50
RESAMPLE_SPREAD_MAX = 5


def load_fixture(name: str, path: str | None = None):
    src = Path(path) if path else SYNTHETIC / FIXTURES[name]
    conv, _parser = parse(src.read_text(encoding="utf-8"), name, None, src.name, ParseOptions())
    return conv, conv.events()


def profile_for(name: str, mbti: str | None = None) -> Profile:
    other = Person(alias="对方", mbti=mbti) if mbti else Person(alias="对方")
    return Profile(me=Person(alias="我"), other=other, goal="invite")


def _snapshot(rep) -> str:
    """One string standing for everything a user would see as the score."""
    dims = "|".join(f"{d}={rep.affinity.dims[d].score}" for d in DIMENSIONS if d in rep.affinity.dims)
    return f"total={rep.affinity.total};raw={rep.affinity.raw_total};{dims}"


def _median_or_none(values: list[int | None]) -> float | None:
    real = [v for v in values if v is not None]
    return statistics.median(real) if real else None


def _resample_totals(conv, events, profile, n: int, frac: float, seed: int) -> list[int | None]:
    """Drop a share of the messages and recompute: a score that depends on one luckily-kept line
    is not stable enough to show as a number."""
    rng = random.Random(seed)
    keep = max(1, len(events) - max(1, round(len(events) * frac)))
    out: list[int | None] = []
    for _ in range(n):
        idx = sorted(rng.sample(range(len(events)), min(keep, len(events))))
        out.append(run_rules(conv, [events[i] for i in idx], profile).affinity.total)
    return out


def render_markdown(mode: str, per_fixture: dict[str, dict], resample: int, repeat: int, seed: int, frac: float) -> str:
    import datetime as _dt

    L = [
        "# 好感度稳定性（规则层 / 模型层）",
        "",
        f"- 日期：{_dt.date.today().isoformat()}",
        f"- 模式：`{mode}`" + (f" · 每 fixture 重复 {repeat} 轮" if mode == "byok" else f" · 重采样 {resample} 次（各丢弃 {frac:.0%} 消息，seed={seed}）"),
        "- 口径：合成 fixture 上的稳定性，不是对真实关系的判断能力。稳定性是可信度的下限，不是上限。",
        "",
        "| fixture | total | 五维 | 确定性 | 重采样极差 | 验收 |",
        "|---|---:|---|---|---:|---|",
    ]
    for name, d in per_fixture.items():
        dims = "、".join(f"{k} {v}" for k, v in d["dims"].items())
        spread = d.get("spread")
        verdict = d.get("verdict", "")
        L.append(
            f"| {name} | {d['total']} | {dims} | {d['deterministic']} | "
            f"{'—' if spread is None else f'{spread:.1f}'} | {verdict} |"
        )
    L += [
        "",
        "## 判读",
        "",
        f"- 确定性：同一份材料连跑两次，total 与五维是否逐字相同（`{_snapshot}` 口径）。",
        f"- 重采样极差：{resample} 次随机丢弃 {frac:.0%} 消息后 total 相对中位的最大偏移；超过 {RESAMPLE_SPREAD_MAX} 说明分数挂在个别句子上。",
        f"- 规则层门：warm ≥ {WARM_MIN}、cooling ≤ {COOLING_MAX}、重采样极差 ≤ {RESAMPLE_SPREAD_MAX}。",
    ]
    if mode == "byok":
        L.append(f"- 模型层 {repeat} 轮的 total 极差记在「重采样极差」列（此模式下为重跑极差）。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; only needed for --mode byok")
    ap.add_argument("--mode", choices=("rules", "byok"), default="rules")
    ap.add_argument("--fixtures", default="warm,cooling,stop")
    ap.add_argument("--models", default="", help="comma-separated model ids (byok mode); each runs every fixture")
    ap.add_argument("--resample", type=int, default=20, help="resampling rounds in --mode rules")
    ap.add_argument("--frac", type=float, default=0.1, help="share of messages dropped per resample")
    ap.add_argument("--seed", type=int, default=20260916, help="fixed so the spread is reproducible")
    ap.add_argument("--repeat", type=int, default=3, help="runs per fixture in --mode byok")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between model runs; use it when the endpoint rate-limits a long batch")
    ap.add_argument("--mbti", default=None, help="optional MBTI label for the priors section (never the score)")
    ap.add_argument("--preset", default="deepseek")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--progress", default=None, help="append one line per fixture here so a long run is observable")
    ap.add_argument("--fake", action="store_true", help="offline fake provider (byok mode only): no key, no cost")
    args = ap.parse_args()

    names = [n.strip() for n in args.fixtures.split(",") if n.strip()]
    unknown = [n for n in names if n not in FIXTURES]
    if unknown:
        ap.error(f"未知 fixture {unknown}；可选 {sorted(FIXTURES)}")

    probe = Probe(args.progress)
    # Fixed settings, private data dir: a run must not read whatever the user's environment
    # happens to configure, or two reports of the same material would not be comparable.
    reset_settings(data_dir=private_eval_dir() / "run-affinity-stability")

    providers: list[tuple[str, object]] = []
    if args.mode == "byok":
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        if args.fake:
            from rapport.llm.fake import FakeProvider

            providers = [(m or "fake", FakeProvider()) for m in (models or ["fake"])]
            probe.line(f"provider=fake（离线假供应商：不出网、不产生费用）· 模型名仅用于区分列：{'、'.join(m for m, _ in providers)}")
        else:
            from _local import is_local_preset, make_local_provider

            local = is_local_preset(args.preset)
            if not args.key_file and not local:
                ap.error("--mode byok 需要 key_file；只想验证流程请加 --fake")
            if not models:
                ap.error("--mode byok 需要 --models；例如 --models \"DeepSeek V4.1 Flash,glm-5.3-flash\"")
            key, file_url = load_key(Path(args.key_file)) if args.key_file else (None, None)
            preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
            base_url = args.base_url or file_url or PROVIDER_PRESETS[preset].base_url
            for m in models:
                p = make_local_provider(preset, m, base_url, task="synthesize", timeout=args.timeout) if local else OpenAICompatProvider(preset, key, m, base_url, timeout=args.timeout)
                ok, msg = p.ping()
                probe.line(f"[{m}] ping ok={ok} msg={msg}")
                if ok:
                    providers.append((m, p))
            if not providers:
                probe.line("没有任何模型可用，未写报告。")
                return 1

    per_fixture: dict[str, dict] = {}
    for name in names:
        conv, events = load_fixture(name)
        profile = profile_for(name, args.mbti)
        print(f"\n=== {name} · {len(events)} 条消息 · {args.mode} ===", flush=True)

        if args.mode == "rules":
            first = _snapshot(run_rules(conv, events, profile))
            second = _snapshot(run_rules(conv, events, profile))
            deterministic = first == second
            rep = run_rules(conv, events, profile)
            total = rep.affinity.total
            dims = {d: rep.affinity.dims[d].score for d in DIMENSIONS if d in rep.affinity.dims}
            totals = _resample_totals(conv, events, profile, args.resample, args.frac, args.seed)
            med = _median_or_none(totals)
            spread = None
            if med is not None:
                spread = max(abs((t if t is not None else med) - med) for t in totals)
            missing = sum(1 for t in totals if t is None)
            # A missing total is not a passing grade: `None` means the gates withheld the number
            # (too little evidence, or a boundary), which is a different statement from "≤ 50".
            blocked = rep.affinity.suppressed_reason or rep.affinity.insufficient_reason
            gates = []
            if total is None:
                gates.append(f"未出分（{'边界抑制' if rep.affinity.suppressed_reason else '证据不足'}），不判过/不过")
            elif name == "warm":
                gates.append(f"warm ≥ {WARM_MIN}: {'过' if total >= WARM_MIN else '未过'}")
            elif name == "cooling":
                gates.append(f"cooling ≤ {COOLING_MAX}: {'过' if total <= COOLING_MAX else '未过'}")
            gates.append(
                f"重采样 ≤ {RESAMPLE_SPREAD_MAX}: "
                + ("过" if spread is not None and spread <= RESAMPLE_SPREAD_MAX else "未过" if spread is not None else "不可判（多数重采样未出分）")
            )
            verdict = " · ".join(gates)
            per_fixture[name] = {
                "total": total,
                "raw_total": rep.affinity.raw_total,
                "dims": dims,
                "deterministic": "是" if deterministic else "**否**",
                "spread": spread,
                "verdict": verdict,
                "blocked": blocked,
            }
            probe.line(f"  total={total} raw={rep.affinity.raw_total} dims={dims}")
            if blocked:
                probe.line(f"  未出分原因：{blocked}")
            probe.line(f"  确定性={'是' if deterministic else '否'}")
            probe.line(f"  重采样 {args.resample} 次（丢 {args.frac:.0%}）：中位 {med} 极差 {spread} 缺分 {missing} 次")
            probe.line(f"  验收：{verdict}")
            probe.done(f"DONE fixture={name} mode=rules total={total} spread={spread} deterministic={deterministic}")
        else:
            for model, provider in providers:
                label = name if len(providers) == 1 else f"{model} · {name}"
                totals: list[int | None] = []
                statuses: list[str] = []
                for i in range(1, max(1, args.repeat) + 1):
                    if args.sleep:
                        time.sleep(args.sleep)
                    ctx = ByokContext(
                        provider=provider,
                        payload=build_payload(conv, events, window_size=30, overlap=5),
                        versions_extra={"provider": provider.info.preset, "model": provider.info.model, "price_date": provider.info.price_date},
                        preset=provider.info.preset,
                        run_intent=False,  # intent readings never enter the affinity total
                    )
                    rep = run_byok(conv, events, profile, ctx)
                    totals.append(rep.affinity.total)
                    statuses.append(rep.ai_status)
                    err = getattr(rep, "ai_error", None)
                    probe.line(f"  [{label} {i}/{args.repeat}] total={rep.affinity.total} raw={rep.affinity.raw_total} ai={rep.ai_status}" + (f" ({err})" if err else ""))
                real = [t for t in totals if t is not None]
                spread = (max(real) - min(real)) if len(real) > 1 else None
                # A round whose synthesis failed falls back to the rules total (run_byok's design), so
                # its number is not the model's number: say so next to the spread instead of hiding it.
                fallbacks = sum(1 for st in statuses if st != "completed")
                verdict = f"total 极差 ≤ {RESAMPLE_SPREAD_MAX}: {'过' if spread is not None and spread <= RESAMPLE_SPREAD_MAX else '未过或样本不足'}"
                if fallbacks:
                    verdict += f" · **{fallbacks}/{len(statuses)} 轮 AI 综合失败，回退规则分**（极差含回退轮）"
                per_fixture[label] = {
                    "total": real[0] if real else None,
                    "dims": {d: None for d in DIMENSIONS},
                    "deterministic": "—",
                    "spread": spread,
                    "verdict": verdict,
                }
                probe.line(f"  {label}：{args.repeat} 轮 total={totals} 极差={spread} ai={statuses}")
                probe.done(f"DONE model={model} fixture={name} mode=byok totals={totals} spread={spread} ai_failed={fallbacks}")

    from datetime import date

    out = ensure_private_out(args.out, f"affinity_stability-{args.mode}-{date.today().isoformat()}.md")
    out.write_text(render_markdown(args.mode, per_fixture, args.resample, args.repeat, args.seed, args.frac), encoding="utf-8")
    probe.line(f"\n报告写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
