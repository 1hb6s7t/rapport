"""Two-person adjudication tooling for the intent evaluation set (docs/EVAL.md §11).

The measurement's weak point is not the model, it is that the gold labels have a single author.
This makes the fix executable:

    # 1. hand two independent reviewers one sheet each (no labels shown, so no anchoring)
    uv run python ../scripts/adjudicate.py export --out ../docs/adjudication

    # 2. compare their sheets: agreement per item, Cohen's kappa on exact-set match, disputes listed
    uv run python ../scripts/adjudicate.py compare a.csv b.csv --out ../docs/adjudication/report.md

    # 3. freeze the agreed labels for the harness (disputes keep the author's label, flagged)
    uv run python ../scripts/adjudicate.py freeze a.csv b.csv --out ../fixtures/eval_labels_frozen.json

    # 4. score against the frozen set instead of the built-in labels
    uv run python ../scripts/intent_eval.py <key> --labels ../fixtures/eval_labels_frozen.json

Nothing here talks to a model: adjudication must be done by people.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from intent_eval import PROBES  # noqa: E402

from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS  # noqa: E402

TAG_SET = set(PRAGMATIC_TAGS) - {"不确定"}
EMOTION_SET = set(EMOTIONS) - {"不确定"}


def scenario_id(index: int) -> str:
    """Stable id: the probe list order is frozen (appending is fine, reordering is not)."""
    return f"s{index + 1:03d}"


def export(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = ["id", "surface", "context", "pragmatic_tags", "emotions", "notes"]
    for reviewer in ("A", "B"):
        path = out_dir / f"reviewer_{reviewer}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(header)
            for i, (surface, context, _tags, _emotions) in enumerate(PROBES):
                w.writerow([scenario_id(i), surface, " / ".join(context), "", "", ""])
    (out_dir / "README.md").write_text(
        "# 双人裁定工作区\n\n"
        "1. 把 `reviewer_A.csv` 与 `reviewer_B.csv` 分别交给两名评审，**互相看不到对方**。\n"
        "2. 每行在 `pragmatic_tags` / `emotions` 里填写标签，多个用 `|` 分隔；\n"
        f"   语用标签只能取自：{'、'.join(sorted(TAG_SET))}\n"
        f"   情绪只能取自：{'、'.join(sorted(EMOTION_SET))}\n"
        "3. 不确定就留空，**不要猜**：留空会记为弃答，这是合法的判断。\n"
        "4. 认为金标签本身有争议的，在 `notes` 里写明理由——争议条目必须保留，不允许删题。\n\n"
        "两张表都不显示项目作者的原标签，避免锚定效应。\n",
        encoding="utf-8",
    )
    print(f"exported {len(PROBES)} scenarios x2 -> {out_dir}")
    return 0


def _parse_labels(cell: str) -> set[str]:
    return {t.strip() for t in (cell or "").replace("，", ",").replace(",", "|").split("|") if t.strip()}


def _read(path: Path) -> dict[str, dict[str, set[str]]]:
    rows: dict[str, dict[str, set[str]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows[str(row["id"])] = {
                "tags": _parse_labels(row.get("pragmatic_tags", "")),
                "emotions": _parse_labels(row.get("emotions", "")),
                "notes": (row.get("notes") or "").strip(),
            }
    return rows


def _kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's kappa between rater A and rater B on one binary decision."""
    n = len(a)
    if n == 0:
        return 0.0
    agree = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    pa1, pb1 = sum(a) / n, sum(b) / n
    expected = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    return 0.0 if expected == 1 else (agree - expected) / (1 - expected)


def per_label_kappa(A: dict, B: dict, ids: list[str], labels: set[str]) -> dict[str, float]:
    """One kappa per label, comparing A's and B's presence/absence of that label on every item.

    Multi-label sets cannot be fed to a single Cohen's kappa; the earlier version compared the
    'exact match' series against the 'overlap' series, which is not an inter-rater statistic at
    all. This is the standard per-label decomposition; the mean over labels with support is the
    headline number.
    """
    out: dict[str, float] = {}
    for label in sorted(labels):
        a = [label in A.get(i, {}).get("tags", set()) for i in ids]
        b = [label in B.get(i, {}).get("tags", set()) for i in ids]
        if not any(a) and not any(b):
            continue
        out[label] = _kappa(a, b)
    return out


def compare(path_a: Path, path_b: Path, out: Path | None) -> int:
    A, B = _read(path_a), _read(path_b)
    ids = sorted(set(A) | set(B))
    exact, partial, disputed = [], [], []
    per_tag = Counter()
    for sid in ids:
        a, b = A.get(sid, {}), B.get(sid, {})
        ta, tb = a.get("tags", set()), b.get("tags", set())
        exact.append(ta == tb)
        partial.append(bool(ta & tb))
        for t in ta | tb:
            per_tag[t] += 1 if t in ta and t in tb else 0
        if ta != tb or a.get("emotions") != b.get("emotions"):
            disputed.append((sid, sorted(ta), sorted(tb), a.get("notes", "") or b.get("notes", "")))
    n = len(ids) or 1
    exact_rate = sum(exact) / n
    partial_rate = sum(partial) / n
    kappas = per_label_kappa(A, B, ids, TAG_SET)
    kappa = sum(kappas.values()) / len(kappas) if kappas else 0.0
    lines = [
        "# 双人裁定报告",
        "",
        f"- 条目：{len(ids)}",
        f"- 语用标签完全一致：{sum(exact)}/{len(ids)} = {exact_rate:.2f}；有重叠：{sum(partial)}/{len(ids)} = {partial_rate:.2f}",
        f"- Cohen's kappa（逐标签 A vs B，取有支持的标签均值）：**{kappa:.3f}**",
        "",
        "| 标签 | kappa |",
        "|---|---:|",
        *[f"| {t} | {k:.3f} |" for t, k in kappas.items()],
        "",
        "## 分歧条目（必须仲裁，不许删除）",
        "",
        "| id | 这句话 | 评审 A | 评审 B | 备注 |",
        "|---|---|---|---|---|",
    ]
    for sid, ta, tb, note in disputed:
        surface = PROBES[int(sid[1:]) - 1][0] if sid[1:].isdigit() and int(sid[1:]) - 1 < len(PROBES) else ""
        lines.append(f"| {sid} | {surface} | {'、'.join(ta) or '—'} | {'、'.join(tb) or '—'} | {note} |")
    lines += [
        "",
        "## 判读",
        "",
        "- kappa ≥ 0.8 视为标注可复现；0.6–0.8 需要讨论分歧条目的判定标准；< 0.6 说明任务定义本身需要先收紧。",
        "- 分歧条目保留在集合中并标注，因为删掉有争议的题会掩盖分歧，而不是解决它。",
    ]
    text = "\n".join(lines) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report -> {out}")
    print(text)
    return 0


def freeze(path_a: Path, path_b: Path, out: Path) -> int:
    A, B = _read(path_a), _read(path_b)
    frozen, carried, flagged = {}, [], []
    for i, (surface, _ctx, author_tags, author_emotions) in enumerate(PROBES):
        sid = scenario_id(i)
        a, b = A.get(sid, {}), B.get(sid, {})
        ta, tb = a.get("tags", set()), b.get("tags", set())
        if ta and ta == tb:
            frozen[sid] = {"pragmatic_tags": sorted(ta), "emotions": sorted(b.get("emotions", set())), "source": "adjudicated"}
        else:
            frozen[sid] = {"pragmatic_tags": list(author_tags), "emotions": list(author_emotions), "source": "author", "disputed": True}
            carried.append(sid)
            flagged.append((sid, surface))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "format": "rapport-eval-labels-v1",
                "note": "由 adjudicate.py freeze 生成：仅当两名评审完全一致时采用裁定标签；其余沿用作者标签并标记 disputed。",
                "counts": {"adjudicated": len(frozen) - len(carried), "author_carried": len(carried)},
                "labels": frozen,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"frozen labels -> {out}（裁定 {len(frozen) - len(carried)} 条，沿用作者标签 {len(carried)} 条）")
    for sid, surface in flagged[:10]:
        print(f"  仍存争议：{sid} {surface}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="导出两份互不可见的评审表")
    e.add_argument("--out", required=True)
    c = sub.add_parser("compare", help="比较两份评审表并输出一致性")
    c.add_argument("sheet_a")
    c.add_argument("sheet_b")
    c.add_argument("--out")
    f = sub.add_parser("freeze", help="冻结可用标签（分歧条目沿用作者标签并标记）")
    f.add_argument("sheet_a")
    f.add_argument("sheet_b")
    f.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "export":
        return export(Path(args.out))
    if args.cmd == "compare":
        return compare(Path(args.sheet_a), Path(args.sheet_b), Path(args.out) if args.out else None)
    return freeze(Path(args.sheet_a), Path(args.sheet_b), Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
