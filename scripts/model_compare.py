"""Run the intent harness against several models on the same slice, and compare them.

Why this exists: which model becomes the default is a decision that has to rest on the frozen
60-case batch B, not on impressions. `intent_eval.py` measures one model; this drives the same
harness (same probes, same code path, same scoring) for each model in turn and puts the results
side by side, so the comparison cannot differ by harness.

Reporting rules it inherits from docs/EVAL.md §14: the header names the slice and the number of
cases that actually ran, a failed call stays visible in the tally instead of being dropped, and
the run ends with a DONE line. Any paid run is preceded by `--fake --limit 1` (EVAL §13).

Usage:
    uv run python ../scripts/model_compare.py "C:/path/API KEY.txt" \
        --models "DeepSeek V4.1 Flash,qwen3-max" --offset 60 --limit 60 --repeat 3
    uv run python ../scripts/model_compare.py --fake --models a,b --limit 1 --detail
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import intent_eval as ie  # noqa: E402
from _private import ensure_private_out  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import PROVIDER_PRESETS  # noqa: E402
from rapport.llm.fake import FakeProvider  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402

# The metrics worth comparing across models, and the direction that is better for each.
METRIC_KEYS = (
    ("macro_f1_raw", "macro-F1(原始)", "up"),
    ("macro_f1_shipped", "macro-F1(上线后)", "up"),
    ("superset_hit_rate_raw", "全标签命中(原始)", "up"),
    ("abstain_rate", "弃答率", "flat"),
    ("high_conf_error_rate", "高置信度误判率", "down"),
)


def _mean_span(scores: list[dict], key: str) -> tuple[float, float, float]:
    vals = [s[key] for s in scores]
    return sum(vals) / len(vals), min(vals), max(vals)


def render_compare_markdown(
    per_model: dict[str, dict[str, list[dict]]],
    scores_by_model: dict[str, dict[str, list[dict]]],
    calls: int,
    failed: int,
    reasons: Counter,
    cases: int,
    slice_note: str,
    reps: int,
    conditions: list[str],
    detail: bool,
) -> str:
    import datetime as _dt

    L = [
        "# 多模型对比（语义情绪）",
        "",
        f"- 日期：{_dt.date.today().isoformat()}",
        f"- 模型：{'、'.join(f'`{m}`' for m in per_model)}",
        ie._slice_line(cases),
        f"- 切片：{slice_note or '全量（未使用 --offset/--limit）'}",
        f"- 条件：{', '.join(conditions)} · 每条件重复 {reps} 轮",
        f"- 共 {calls} 次模型调用，其中失败 {failed} 次"
        + (f"（原因：{'、'.join(f'{k}×{v}' for k, v in reasons.most_common())}）" if reasons else "（无失败）"),
        "- 口径：合成对照集上的工程指标，不是产品准确率。两模型差值小于噪声底（约 ±0.02 macro-F1）时不构成优劣证据；"
        "`--repeat 3` 的区间重叠同样不构成证据。",
        "",
        "## 汇总（各条件 × 各模型）",
        "",
    ]
    for metric, title, direction in METRIC_KEYS:
        arrow = {"up": "越高越好", "down": "越低越好", "flat": "中性"}[direction]
        L += [f"### {title}（{arrow}）", "", "| 模型 | " + " | ".join(conditions) + " |", "|---|" + "---:|" * len(conditions)]
        for model, by_cond in scores_by_model.items():
            cells = []
            for c in conditions:
                scores = by_cond.get(c)
                if not scores:
                    cells.append("—")
                    continue
                mean, lo, hi = _mean_span(scores, metric)
                cells.append(f"{mean:.3f} [{lo:.3f}, {hi:.3f}]" if reps > 1 else f"{mean:.3f}")
            L.append(f"| {model} | " + " | ".join(cells) + " |")
        L.append("")

    L += ["## 失败调用", ""]
    if not reasons:
        L.append("本次运行没有失败调用。")
    else:
        L += ["| 原因 | 次数 |", "|---|---:|"]
        for reason, n in reasons.most_common():
            L.append(f"| {reason} | {n} |")

    if detail:
        for model, by_cond in per_model.items():
            for key, rows in by_cond.items():
                ie._case_table(L, {f"{model} · {key}": rows})
        for model, by_cond in per_model.items():
            for key, rows in by_cond.items():
                ie._case_detail_table(L, {f"{model} · {key}": rows})
    else:
        L += ["", "（未附逐条表；需要时加 `--detail` 重跑或复看 `--progress` 日志。）"]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; omit it when using --fake")
    ap.add_argument("--models", default="", help="comma-separated model ids, each run over the same slice")
    ap.add_argument("--preset", default="deepseek", help="provider preset from rapport.config.PROVIDER_PRESETS")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--conditions", default="context")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0, help="e.g. --offset 60 --limit 60 for batch B")
    ap.add_argument("--repeat", type=int, default=1, help="re-run each condition N times; report mean and range")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--patterns", action="store_true")
    ap.add_argument("--labels", default=None)
    ap.add_argument("--progress", default=None, help="append one line per completed case here")
    ap.add_argument("--detail", action="store_true", help="also write the per-case and per-case-basis tables (long)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fake", action="store_true", help="offline fake provider: no key, no network, no cost")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.fake:
        models = models or ["fake-a", "fake-b"]
        print(f"provider=fake（离线假供应商：不出网、不产生费用；本次模型名仅用于区分列：{'、'.join(models)}）")
    else:
        if not args.key_file:
            ap.error("需要 key_file；只想验证流程请用 --fake")
        if not models:
            ap.error("--models 不能为空；例如 --models \"DeepSeek V4.1 Flash,qwen3-max\"")
    key, file_url = load_key(Path(args.key_file)) if args.key_file else (None, None)

    conditions = [c for c in args.conditions.split(",") if c]
    library = ie.load_library()
    patterns = [p.prompt_dict() for p in ie.load_patterns()] if args.patterns else []
    probes, label_stats = ie.apply_frozen_labels(ie.PROBES, args.labels)
    if args.labels:
        print(f"标签来源：裁定 {label_stats['adjudicated']} 条 · 沿用作者 {label_stats['author_carried']} 条 · 未覆盖 {label_stats['missing']} 条")
    probes = probes[args.offset :]
    probes = probes[: args.limit] if args.limit else probes
    slice_note = ""
    if args.offset or args.limit:
        slice_note = f"--offset {args.offset}" + (f" --limit {args.limit}" if args.limit else "")
    print(f"models={len(models)}  scenarios={len(probes)}  conditions={','.join(conditions)}  repeat={args.repeat}")

    probe_out = ie.Probe(args.progress)
    per_model: dict[str, dict[str, list[dict]]] = {}
    scores_by_model: dict[str, dict[str, list[dict]]] = {}
    reasons: Counter = Counter()
    calls = failed = 0

    for model in models:
        if args.fake:
            provider = FakeProvider()
        else:
            preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
            base_url = args.base_url or file_url or PROVIDER_PRESETS[preset].base_url
            provider = OpenAICompatProvider(preset, key, model, base_url, timeout=args.timeout)
            ok, msg = provider.ping()
            probe_out.line(f"[{model}] ping ok={ok} msg={msg}")
            if not ok:
                # A model that cannot be reached is reported and skipped, never silently absent
                # from the comparison table.
                reasons[f"ping_failed:{model}"] += 1
                failed += 1
                continue

        by_cond: dict[str, list[dict]] = {}
        score_cond: dict[str, list[dict]] = {}
        for rep in range(max(1, args.repeat)):
            tag = f" rep={rep + 1}/{args.repeat}" if args.repeat > 1 else ""
            if args.repeat > 1:
                probe_out.line(f"\n===== {model} · 第 {rep + 1}/{args.repeat} 轮 =====")
            for condition in conditions:
                rows = ie._run_condition(provider, probes, condition, library, probe_out, patterns, args.sleep, f" {model}{tag}")
                key_ = condition if args.repeat == 1 else f"{condition}#rep{rep + 1}"
                by_cond[key_] = rows
                score_cond.setdefault(condition, []).append(ie.score(rows))
                calls += len(rows)
                n_failed = sum(1 for r in rows if r.get("error"))
                failed += n_failed
                for r in rows:
                    if r.get("error"):
                        reasons[r["error"].split(":")[0]] += 1
        per_model[model] = by_cond
        scores_by_model[model] = score_cond

    print("\n================ 对比（均值 [最小, 最大]）================")
    for model, by_cond in scores_by_model.items():
        for condition, scores in by_cond.items():
            macro, lo, hi = _mean_span(scores, "macro_f1_raw")
            shipped, _, _ = _mean_span(scores, "macro_f1_shipped")
            print(f"{model:<24} {condition:<10} macro-F1 原始 {macro:.3f} [{lo:.3f}, {hi:.3f}] · 上线 {shipped:.3f} · n={len(scores)}")
    if reasons:
        print(f"失败 {failed}/{calls} 次：{'、'.join(f'{k}×{v}' for k, v in reasons.most_common())}")

    if per_model:
        from datetime import date

        out = ensure_private_out(args.out, f"model_compare-{date.today().isoformat()}.md")
        out.write_text(
            render_compare_markdown(per_model, scores_by_model, calls, failed, reasons, len(probes), slice_note, args.repeat, conditions, args.detail),
            encoding="utf-8",
        )
        print(f"\n报告写入 {out}")
    else:
        print("\n没有任何模型跑成功，未写报告。")

    probe_out.done(f"DONE models={len(scores_by_model)}/{len(models)} cases={len(probes)} calls={calls} failed={failed}")
    return 0 if scores_by_model else 1


if __name__ == "__main__":
    raise SystemExit(main())
