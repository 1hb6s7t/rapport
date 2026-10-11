"""Does the advice survive its own validation? (plan §2, stage 3's gate)

The playbook is the part of a report a user acts on, so the question that matters is not "did the
model produce JSON" but "how much of what it proposed was structurally usable, and what got thrown
away". This drives the synthesis stage directly (extract → signals → synthesize → validate) so the
discard counts are visible instead of being absorbed by the pipeline.

It reports, per model and fixture:

- proposed / kept / survival rate — `validate_synthesis` drops actions without `stop_when` and
  advancing actions under a boundary; those counts come from `Validated.dropped`;
- independent re-checks of the same rules on the *surviving* actions, because "the validator ran"
  and "the output is clean" are two different claims;
- how often a repair round was needed.

Plan §3's gate is survival ≥85% on all three models; below 60% the answer is to derive
`trigger_ids` programmatically instead of asking the model for them.

Usage:
    uv run python ../scripts/synth_compliance.py --fake --models a,b --repeat 2
    uv run python ../scripts/synth_compliance.py "C:/path/API KEY.txt" --models "DeepSeek V4.1 Flash,qwen3-max"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, private_eval_dir  # noqa: E402
from _probe import Probe  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import PROVIDER_PRESETS, reset_settings  # noqa: E402
from rapport.features.compute import compute_features  # noqa: E402
from rapport.llm.extract import build_payload, run_extraction  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402
from rapport.llm.synthesize import build_synth_user, run_synthesis  # noqa: E402
from rapport.llm.validate import ADVANCE_WORDS, ValidationFailed, validate_synthesis  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC = REPO / "fixtures" / "synthetic"
FIXTURES = {
    "warm": "paste_ab_warm.txt",
    "cooling": "paste_ab_cooling.txt",
    "stop": "paste_ab_stop_contact.txt",
}
SURVIVAL_GATE = 0.85
SURVIVAL_FLOOR = 0.60


def _raw_actions(data: dict) -> list[dict]:
    pb = data.get("playbook") or {}
    return [a for a in (pb.get("actions") or []) if isinstance(a, dict)]


def run_one(provider, conv, events, profile, window_size: int) -> dict:
    """One synthesis run, returning the compliance facts. Failures are returned, not raised."""
    payload = build_payload(conv, events, window_size=window_size, overlap=5)
    f = compute_features(conv, events)
    calls = 0
    try:
        results = []
        for i in range(len(payload.lines_by_window)):
            wr, _usage = run_extraction(provider, payload, i, None)
            results.append(wr)
            calls += 1
        valid_ids = {i for r in results for i in r.msg_ids}
        user = build_synth_user(f, results, profile)
        data, _usage = run_synthesis(provider, user)
        calls += 1
        repair = False
        try:
            validated = validate_synthesis(data, valid_ids)
        except ValidationFailed as e:
            repair = True
            data, _usage = run_synthesis(provider, user, repair_error=str(e))
            calls += 1
            validated = validate_synthesis(data, valid_ids)
    except (LLMError, ValidationFailed) as e:
        return {"error": f"{getattr(e, 'code', type(e).__name__)}: {getattr(e, 'msg', e)}", "calls": calls}

    proposed = _raw_actions(data)
    kept = list(validated.playbook.actions) if validated.playbook else []
    # Independent re-check of the rules the validator is supposed to enforce. If these ever go
    # non-zero, the validator itself is the bug — not the model.
    no_stop = sum(1 for a in kept if not (a.stop_when or "").strip())
    bad_basis = sum(1 for a in kept if any(b not in valid_ids for b in a.basis_ids))
    restrictive = validated.boundary is not None and getattr(validated.boundary, "value", "none") != "none"
    advance_leak = sum(1 for a in kept if restrictive and ADVANCE_WORDS.search(f"{a.goal} {a.action}"))
    return {
        "error": None,
        "calls": calls,
        "repair": repair,
        "proposed": len(proposed),
        "kept": len(kept),
        "survival": (len(kept) / len(proposed)) if proposed else None,
        "situations": len(validated.playbook.situations) if validated.playbook else 0,
        "options_kept": sum(len(s.options) for s in validated.playbook.situations) if validated.playbook else 0,
        "options_dropped": sum(v for k, v in validated.dropped.items() if k.startswith("options_") or k.startswith("situations_")),
        "no_stop": no_stop,
        "bad_basis": bad_basis,
        "advance_leak": advance_leak,
        "restrictive": restrictive,
        "dropped": dict(validated.dropped),
        "errors": list(validated.errors),
        "stop_when_kept": sum(1 for a in kept if (a.stop_when or "").strip()),
        "with_alternatives": sum(1 for a in kept if a.alternatives),
    }


def render_markdown(rows: list[dict], window_size: int, repeat: int) -> str:
    import datetime as _dt

    L = [
        "# 攻略选项的校验存活率",
        "",
        f"- 日期：{_dt.date.today().isoformat()}",
        f"- 每对（模型 × fixture）重复 {repeat} 轮 · 抽取窗口 {window_size} 条",
        "- 口径：**结构合规率**，不是建议质量。存活率只说明模型给的选项能通过校验、能被用户照做；"
        "它完全不说明建议本身对不对。",
        "",
        "| 模型 | fixture | 提出 | 存活 | 存活率 | 情境 | 选项存活/丢弃 | 需修复轮 | 无 stop_when | 非法证据 | 边界下推进 | 丢弃明细 |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|",
    ]
    for r in rows:
        if r["error"]:
            L.append(f"| {r['model']} | {r['fixture']} | — | — | — | — | — | — | — | — | — | 失败：{r['error']} |")
            continue
        surv = "—" if r["survival"] is None else f"{r['survival']:.0%}"
        dropped = "、".join(f"{k}×{v}" for k, v in r["dropped"].items()) or "—"
        L.append(
            f"| {r['model']} | {r['fixture']} | {r['proposed']} | {r['kept']} | {surv} | {r.get('situations', 0)} | {r.get('options_kept', 0)}/{r.get('options_dropped', 0)} | {'是' if r['repair'] else ''} | "
            f"{r['no_stop']} | {r['bad_basis']} | {r['advance_leak']} | {dropped} |"
        )
    # Failure rows carry only {model, fixture, error, calls}; indexing "survival" on them raised
    # KeyError and lost the whole report the first time a local model truncated (2026-09-20).
    rates = [r["survival"] for r in rows if r.get("survival") is not None]
    n_failed = sum(1 for r in rows if r.get("error"))
    L += ["", "## 判读", ""]
    if rates:
        mean = sum(rates) / len(rates)
        L.append(f"- 平均存活率 {mean:.0%}（{len(rates)} 对模型×fixture）；门 ≥{SURVIVAL_GATE:.0%}，低于 {SURVIVAL_FLOOR:.0%} 则 `trigger_ids` 改为程序反查。")
    else:
        L.append("- 本次没有任何一对跑出存活率（全部失败），不能据此判断模型。")
    if n_failed:
        L.append(
            f"- **失败 {n_failed}/{len(rows)} 轮**（截断 / 非法 JSON / 调用错误，原因在表内「失败：」列）。"
            "平均存活率只按成功轮计算；一个经常失败的模型即使存活率 100% 也不能算过门。"
        )
    L += [
        "- 「提出」是模型最终给出的 action 条数，「存活」是通过校验进入报告的条数；两者之差等于丢弃明细之和。",
        "- 后三列是**独立复核**：它们由本脚本重算，若不为 0，问题在校验器而不在模型。",
        "- 「需修复轮」为是，说明第一次输出不合规、用了一次修复重试——这本身是模型稳定性的信号。",
    ]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; omit it when using --fake")
    ap.add_argument("--models", default="", help="comma-separated model ids; each runs every fixture")
    ap.add_argument("--fixtures", default="warm,cooling,stop")
    ap.add_argument("--repeat", type=int, default=1, help="runs per (model, fixture) pair")
    ap.add_argument("--window", type=int, default=30, help="messages per extraction window")
    ap.add_argument("--preset", default="deepseek")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--progress", default=None, help="append one line per finished run here")
    ap.add_argument("--fake", action="store_true", help="offline fake provider: no key, no network, no cost")
    args = ap.parse_args()

    probe = Probe(args.progress)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.fake:
        models = models or ["fake-a"]
        probe.line(f"provider=fake（离线假供应商：不出网、不产生费用；模型名仅用于区分列：{'、'.join(models)}）")
    else:
        from _local import is_local_preset

        if not args.key_file and not is_local_preset(args.preset):
            ap.error("需要 key_file；只想验证流程请用 --fake")
        if not models:
            ap.error("--models 不能为空；例如 --models \"DeepSeek V4.1 Flash,qwen3-max\"")
    key, file_url = load_key(Path(args.key_file)) if args.key_file else (None, None)

    names = [n.strip() for n in args.fixtures.split(",") if n.strip()]
    unknown = [n for n in names if n not in FIXTURES]
    if unknown:
        ap.error(f"未知 fixture {unknown}；可选 {sorted(FIXTURES)}")

    reset_settings(data_dir=private_eval_dir() / "run-synth-compliance")
    profile = Profile(me=Person(alias="我"), other=Person(alias="对方"), goal="invite")

    loaded = {}
    for name in names:
        src = SYNTHETIC / FIXTURES[name]
        conv, _parser = parse(src.read_text(encoding="utf-8"), name, None, src.name, ParseOptions())
        loaded[name] = (conv, conv.events())
    probe.line(f"models={len(models)}  fixtures={len(names)}  repeat={args.repeat}")
    rows: list[dict] = []
    total_calls = 0
    for model in models:
        if args.fake:
            from rapport.llm.fake import FakeProvider

            provider = FakeProvider()
        else:
            from _local import is_local_preset, make_local_provider

            preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
            base_url = args.base_url or file_url or PROVIDER_PRESETS[preset].base_url
            if is_local_preset(preset):
                provider = make_local_provider(preset, model, base_url, task="synthesize", timeout=args.timeout)
            else:
                provider = OpenAICompatProvider(preset, key, model, base_url, timeout=args.timeout)
            ok, msg = provider.ping()
            probe.line(f"[{model}] ping ok={ok} msg={msg}")
            if not ok:
                rows.append({"model": model, "fixture": "—", "error": f"ping 失败：{msg}", "calls": 0})
                continue
        for name in names:
            conv, events = loaded[name]
            for rep in range(1, max(1, args.repeat) + 1):
                out = run_one(provider, conv, events, profile, args.window)
                total_calls += out.get("calls", 0)
                rec = {"model": model, "fixture": name, **out}
                rows.append(rec)
                if out["error"]:
                    probe.line(f"  [{model} · {name} · {rep}] 失败 {out['error']}")
                else:
                    surv = "—" if out["survival"] is None else f"{out['survival']:.0%}"
                    probe.line(
                        f"  [{model} · {name} · {rep}] 提出 {out['proposed']} 存活 {out['kept']} ({surv}) 情境 {out.get('situations', 0)} 选项 {out.get('options_kept', 0)}/{out.get('options_kept', 0) + out.get('options_dropped', 0)} "
                        f"丢弃 {out['dropped'] or '无'} 修复={'是' if out['repair'] else '否'}"
                    )
                    probe.done(f"DONE model={model} fixture={name} rep={rep} kept={out['kept']} proposed={out['proposed']}")

    failed = sum(1 for r in rows if r["error"])
    out_path = ensure_private_out(args.out, "synth_compliance.md")
    out_path.write_text(render_markdown(rows, args.window, args.repeat), encoding="utf-8")
    probe.line(f"\n共 {total_calls} 次模型调用，失败 {failed}/{len(rows)} 对；报告写入 {out_path}")
    return 0 if failed < len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
