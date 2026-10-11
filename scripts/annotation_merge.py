"""Merge the per-batch annotation files into one, and check them before anyone uses them.

Annotating a few hundred batches by hand produces a few hundred files, and every failure mode here
is quiet: a batch saved under the wrong name, a batch never returned at all, a label outside the
fixed list, an index belonging to a different relationship. Each of those would surface much later
as "some metric dropped and nobody knows why". Finding them now costs one command.

Layout it expects (same segment folders as the batch files):

    ~/.rapport/eval/annotations/<段名>/batch_0001.json   ← 模型返回的 JSON 原样存这里
    ~/.rapport/eval/annotations/<段名>/batch_0002.json
    ...

Usage:
    uv run python ../scripts/annotation_merge.py --dir "C:/Users/HP/.rapport/eval/annotations"
    uv run python ../scripts/annotation_merge.py --dir "..." --expect-batches "C:/Users/HP/.rapport/eval/redacted/batches"
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out  # noqa: E402

from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS  # noqa: E402

BOUNDARIES = ("none", "explicit_decline", "stop_contact", "safety_concern")
CONFIDENCE = ("high", "medium", "low")


def _check(msg: dict, where: str, problems: list[str]) -> bool:
    ok = True
    if not isinstance(msg.get("index"), int):
        problems.append(f"{where}: index 不是整数（{msg.get('index')!r}）")
        ok = False
    if msg.get("speaker") not in ("me", "other"):
        problems.append(f"{where}: speaker={msg.get('speaker')!r} 不在 me/other")
        ok = False
    for field, allowed in (("pragmatic_tags", PRAGMATIC_TAGS), ("emotions", EMOTIONS)):
        vals = msg.get(field)
        if not isinstance(vals, list):
            problems.append(f"{where}: {field} 不是数组")
            ok = False
            continue
        for v in vals:
            if v not in allowed:
                problems.append(f"{where}: {field} 词表外标签 {v!r}")
                ok = False
    if msg.get("boundary", "none") not in BOUNDARIES:
        problems.append(f"{where}: boundary={msg.get('boundary')!r} 非法")
        ok = False
    if msg.get("confidence") not in CONFIDENCE:
        problems.append(f"{where}: confidence={msg.get('confidence')!r} 非法")
        ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="root of the per-batch annotation files")
    ap.add_argument("--expect-batches", default=None, help="the batches/ dir from private_redact.py, to report missing batches")
    ap.add_argument("--out", default=None, help="must be outside the repo; defaults to ~/.rapport/eval/")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"找不到目录：{root}", file=sys.stderr)
        return 2
    files = sorted(root.rglob("batch_*.json"))
    if not files:
        print(f"{root} 下没有 batch_*.json", file=sys.stderr)
        return 2

    from datetime import date

    out = ensure_private_out(args.out, f"annotations-merged-{date.today().isoformat()}.json")

    by_segment: dict[str, list[dict]] = defaultdict(list)
    problems: list[str] = []
    per_file: list[dict] = []
    seen: dict[str, set[str]] = defaultdict(set)
    for f in files:
        segment = f.parent.name if f.parent != root else "未分组"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{f.name}: 不是合法 JSON（{e}）——多半是模型返回里混了解释文字或被截断")
            continue
        msgs = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(msgs, list):
            problems.append(f"{f.name}: 没有 messages 数组")
            continue
        good = 0
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and _check(m, f"{f.name}#{i}", problems):
                good += 1
                by_segment[segment].append(m)
                seen[segment].add(str(m.get("index")))
        per_file.append({"file": f.name, "segment": segment, "entries": len(msgs), "kept": good})

    missing: dict[str, list[str]] = {}
    if args.expect_batches:
        exp_root = Path(args.expect_batches)
        for seg_dir in sorted(p for p in exp_root.glob("*") if p.is_dir()):
            expected = {p.stem.replace("batch_", "") for p in seg_dir.glob("batch_*.txt")}
            got = {p.stem.replace("batch_", "") for p in (root / seg_dir.name).glob("batch_*.json")}
            if got or seg_dir.name in by_segment:
                lack = sorted(expected - got)
                if lack:
                    missing[seg_dir.name] = lack

    others = [m for msgs in by_segment.values() for m in msgs if m.get("speaker") == "other"]
    tags = Counter(t for m in others for t in (m.get("pragmatic_tags") or []))
    soft = sum(1 for m in others if m.get("needs_review"))
    media = sum(1 for m in others if m.get("media_unknown"))

    print(f"读取 {len(files)} 个批次文件，{out.name} 将写入 {out.parent}")
    for seg, msgs in sorted(by_segment.items()):
        n_other = sum(1 for m in msgs if m.get("speaker") == "other")
        print(f"  {seg:34s} 共 {len(msgs):>6} 条 · 对方 {n_other:>6} 条")
    if missing:
        print("\n⚠️ 缺失的批次（这些批还没标，或存错了名字）：")
        for seg, lack in missing.items():
            print(f"  {seg}: 缺 {len(lack)} 批 → {', '.join(lack[:12])}{' …' if len(lack) > 12 else ''}")
    print(f"\n格式问题 {len(problems)} 处" + ("：" if problems else "（无）"))
    for p in problems[:30]:
        print("  - " + p)
    if len(problems) > 30:
        print(f"  …… 还有 {len(problems) - 30} 处")
    print(f"\n对方消息合计 {len(others)} 条 · 待复核 {soft}（{soft / len(others):.0%}）· 媒体未观测 {media}" if others else "\n没有对方消息被标注")
    print("标签分布：" + ("、".join(f"{k}×{v}" for k, v in tags.most_common()) or "（空）"))

    out.write_text(
        json.dumps(
            {
                "files": per_file,
                "problems": problems,
                "missing_batches": missing,
                "stats": {
                    "labelled_other": len(others),
                    "needs_review": soft,
                    "media_unknown": media,
                    "tag_counts": dict(tags.most_common()),
                },
                "segments": {k: v for k, v in by_segment.items()},
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n合并结果写入 {out}")
    print(f"DONE files={len(files)} other={len(others)} problems={len(problems)} missing={sum(len(v) for v in missing.values())}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
