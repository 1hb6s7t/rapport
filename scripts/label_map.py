"""Map the open-vocabulary annotation (context-v4) onto the project's fixed label set, and
emit evaluation cases for `intent_eval.py --cases`.

Why a mapping and not a new vocabulary: the external annotator used 3,867 pragmatic labels and
94 emotions. The product ships eight pragmatic tags and seven emotions. To measure the product on
this material at all, each open label has to land on one fixed tag, on **none** ("the fixed set has
no such class" — a legitimate outcome, not an abstention), or on **unmapped** (nobody decided yet).
The map lives in `docs/label_map_v1.json` and is versioned by content hash in every output.

Outputs (private dir only, `_private.ensure_private_out`):
    ~/.rapport/eval/gold/cases-v1.json      the cases (gold + weak strata)
    ~/.rapport/eval/gold/label-map-report-<date>.md   coverage, none share, unmapped list

Usage:
    cd backend
    uv run python ../scripts/label_map.py \
        --annotations "C:/Users/<you>/.rapport/eval/annotations" \
        --lines "C:/Users/<you>/.rapport/eval/redacted" \
        --report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, is_inside_repo, private_eval_dir  # noqa: E402

from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS  # noqa: E402

MAP_PATH = Path(__file__).resolve().parents[1] / "docs" / "label_map_v2.json"
HEAD_RE = re.compile(r"^\[(?P<idx>\d+)\] (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?) (?P<who>我|对方)：(?P<body>.*)$")
NONE = "none"
UNMAPPED = "unmapped"
CONTEXT_LINES = 6


class LabelMap:
    def __init__(self, path: Path = MAP_PATH):
        raw = path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()[:12]
        d = json.loads(raw.decode("utf-8"))
        self.version = d["version"]
        self.prag: dict[str, str | None] = d["pragmatic"]
        self.prag_prefix: list[tuple[str, str | None]] = [tuple(x) for x in d["pragmatic_prefix_rules"]]
        self.emo: dict[str, str | None] = d["emotion"]
        self.boundary_override: dict[str, str] = d.get("boundary_override", {})
        for v in list(self.prag.values()) + [r[1] for r in self.prag_prefix]:
            assert v is None or v in PRAGMATIC_TAGS, v
        for v in self.emo.values():
            assert v is None or v in EMOTIONS, v

    def map_prag(self, label: str) -> tuple[str, str]:
        """Return (fixed_tag | none | unmapped, how)."""
        if label in self.prag:
            v = self.prag[label]
            return (v if v is not None else NONE), "exact"
        for prefix, v in self.prag_prefix:
            if label.startswith(prefix) or prefix in label:
                return (v if v is not None else NONE), f"prefix:{prefix}"
        return UNMAPPED, "unmapped"

    def map_emo(self, label: str) -> tuple[str, str]:
        if label in self.emo:
            v = self.emo[label]
            return (v if v is not None else NONE), "exact"
        return UNMAPPED, "unmapped"


def fold_tags(vals: list[str], mapper) -> tuple[list[str], list[str], list[str]]:
    """Map every open label; return (fixed tags, labels that fell to none, labels unmapped)."""
    fixed: list[str] = []
    nones: list[str] = []
    unm: list[str] = []
    for v in vals or []:
        m, _how = mapper(v)
        if m == NONE:
            nones.append(v)
        elif m == UNMAPPED:
            unm.append(v)
        elif m not in fixed:
            fixed.append(m)
    return fixed, nones, unm


def fold_emotions(vals: list[str], lm: LabelMap) -> tuple[list[str], list[str], list[str]]:
    fixed, nones, unm = fold_tags(vals, lm.map_emo)
    pos = {"开心", "亲近"}
    neg = {"不满", "伤心", "担忧"}
    if any(e in pos for e in fixed) and any(e in neg for e in fixed):
        fixed = ["混合"] + [e for e in fixed if e not in pos | neg]
    return fixed, nones, unm


def read_lines(path: Path) -> dict[int, dict]:
    """index -> {who, when, body, line_no}; multi-line bodies are joined with newlines."""
    L = path.read_text(encoding="utf-8").splitlines()
    heads = [(i, HEAD_RE.match(line)) for i, line in enumerate(L)]
    heads = [(i, m) for i, m in heads if m]
    out: dict[int, dict] = {}
    for pos, (i, m) in enumerate(heads):
        end = heads[pos + 1][0] if pos + 1 < len(heads) else len(L)
        body = m.group("body")
        if end > i + 1:
            body = body + "\n" + "\n".join(L[i + 1 : end])
        out[int(m.group("idx"))] = {"who": m.group("who"), "when": m.group("when"), "body": body, "line_no": i}
    return out


def build_cases(ann_root: Path, lines_root: Path, lm: LabelMap, context_lines: int = CONTEXT_LINES) -> tuple[list[dict], dict]:
    cases: list[dict] = []
    stats = {
        "segments": 0,
        "other_total": 0,
        "prag_exact": 0,
        "prag_prefix": 0,
        "prag_unmapped_msgs": 0,
        "prag_none_only_msgs": 0,
        "emo_unmapped_msgs": 0,
        "emo_none_only_msgs": 0,
        "media_unknown": 0,
        "needs_review": 0,
        "text_mismatch": 0,
        "gold": 0,
        "weak": 0,
        "unmapped_prag_labels": Counter(),
        "unmapped_emo_labels": Counter(),
        "fixed_tag_counts": Counter(),
        "fixed_emotion_counts": Counter(),
        "none_source_labels": Counter(),
    }
    for seg_dir in sorted(p for p in ann_root.glob("*") if p.is_dir() and not p.name.startswith("_")):
        lines_file = lines_root / f"{seg_dir.name}.lines.txt"
        if not lines_file.exists():
            continue
        stats["segments"] += 1
        lines = read_lines(lines_file)
        ordered = sorted(lines)
        pos_of = {idx: i for i, idx in enumerate(ordered)}
        seen: set[int] = set()
        for f in sorted(seg_dir.glob("batch_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for m in data.get("messages") or []:
                if m.get("speaker") != "other" or not isinstance(m.get("index"), int):
                    continue
                idx = m["index"]
                if idx in seen or idx not in lines:
                    continue
                seen.add(idx)
                stats["other_total"] += 1
                src = lines[idx]
                if src["who"] != "对方":
                    continue
                text_ok = (m.get("text") or "").strip() == src["body"].strip() or src["body"].startswith((m.get("text") or "").strip())
                if not text_ok:
                    stats["text_mismatch"] += 1
                tags, nones, unm = fold_tags(m.get("pragmatic_tags") or [], lm.map_prag)
                emos, enones, eunm = fold_emotions(m.get("emotions") or [], lm)
                hows = [lm.map_prag(v)[1] for v in (m.get("pragmatic_tags") or [])]
                stats["prag_exact"] += sum(1 for h in hows if h == "exact")
                stats["prag_prefix"] += sum(1 for h in hows if h.startswith("prefix"))
                if unm:
                    stats["prag_unmapped_msgs"] += 1
                    stats["unmapped_prag_labels"].update(unm)
                if eunm:
                    stats["emo_unmapped_msgs"] += 1
                    stats["unmapped_emo_labels"].update(eunm)
                if not tags and nones and not unm:
                    stats["prag_none_only_msgs"] += 1
                    stats["none_source_labels"].update(nones)
                if not emos and enones and not eunm:
                    stats["emo_none_only_msgs"] += 1
                stats["fixed_tag_counts"].update(tags)
                stats["fixed_emotion_counts"].update(emos)
                boundary = m.get("boundary") or "none"
                for v in m.get("pragmatic_tags") or []:
                    if v in lm.boundary_override and boundary == "none":
                        boundary = lm.boundary_override[v]
                media = bool(m.get("media_unknown"))
                review = bool(m.get("needs_review"))
                stats["media_unknown"] += media
                stats["needs_review"] += review
                # Context: the preceding lines in the same segment, as "A: / B:" for the paste parser.
                p = pos_of[idx]
                ctx_idx = ordered[max(0, p - context_lines) : p]
                context = [f"{'A' if lines[i]['who'] == '我' else 'B'}: {lines[i]['body'].splitlines()[0][:200]}" for i in ctx_idx]
                stratum = "weak" if (media or review or unm or not text_ok) else "gold"
                stats[stratum] += 1
                cases.append(
                    {
                        "id": f"{seg_dir.name}#{idx}",
                        "segment": seg_dir.name,
                        "index": idx,
                        "context": context,
                        "utterance": src["body"].splitlines()[0][:400],
                        "gold_tags": tags if tags else ([NONE] if not unm else []),
                        "gold_emotions": emos if emos else ([NONE] if not eunm else []),
                        "boundary": boundary,
                        "confidence": m.get("confidence"),
                        "needs_review": review,
                        "media_unknown": media,
                        "from_voice": bool(m.get("from_voice")),
                        "open_tags": list(m.get("pragmatic_tags") or []),
                        "open_emotions": list(m.get("emotions") or []),
                        "stratum": stratum,
                    }
                )
    return cases, stats


def render_report(stats: dict, lm: LabelMap, out_cases: Path) -> str:
    n = max(1, stats["other_total"])
    L = [
        "# 开放词表 → 固定词表 映射报告",
        "",
        f"- 日期：{date.today().isoformat()}",
        f"- 映射表：`{lm.version}` 内容哈希 `{lm.digest}`",
        f"- 用例文件：`{out_cases.name}`（{stats['gold']} gold + {stats['weak']} weak）",
        "- 口径：**这是 AI 弱金标签，不是人工金标准**。`none` 表示固定词表无此类，不是弃答；`unmapped` 表示映射表尚未裁定。",
        "",
        "## 覆盖",
        "",
        "| 项 | 数 | 占对方消息 |",
        "|---|---:|---:|",
        f"| 对方消息（已标注且能对回原文） | {stats['other_total']} | 100% |",
        f"| 语用：映射表精确命中（标签次数） | {stats['prag_exact']} | — |",
        f"| 语用：前缀规则命中（标签次数） | {stats['prag_prefix']} | — |",
        f"| 语用：含未映射标签的消息 | {stats['prag_unmapped_msgs']} | {stats['prag_unmapped_msgs'] / n:.1%} |",
        f"| 语用：只落到 none 的消息 | {stats['prag_none_only_msgs']} | {stats['prag_none_only_msgs'] / n:.1%} |",
        f"| 情绪：含未映射标签的消息 | {stats['emo_unmapped_msgs']} | {stats['emo_unmapped_msgs'] / n:.1%} |",
        f"| 情绪：只落到 none 的消息 | {stats['emo_none_only_msgs']} | {stats['emo_none_only_msgs'] / n:.1%} |",
        f"| 媒体不可观测 | {stats['media_unknown']} | {stats['media_unknown'] / n:.1%} |",
        f"| 标注者标 needs_review | {stats['needs_review']} | {stats['needs_review'] / n:.1%} |",
        f"| 标注 text 与原文不一致 | {stats['text_mismatch']} | {stats['text_mismatch'] / n:.1%} |",
        "",
        "## 固定语用标签分布（映射后，多标签计次）",
        "",
        "| 标签 | 次数 |",
        "|---|---:|",
    ]
    for k in PRAGMATIC_TAGS:
        L.append(f"| {k} | {stats['fixed_tag_counts'].get(k, 0)} |")
    L += ["", "## 固定情绪分布（映射后）", "", "| 情绪 | 次数 |", "|---|---:|"]
    for k in EMOTIONS:
        L.append(f"| {k} | {stats['fixed_emotion_counts'].get(k, 0)} |")
    L += ["", "## 落到 none 的来源标签（前 40）", "", "| 开放标签 | 次数 |", "|---|---:|"]
    for k, v in stats["none_source_labels"].most_common(40):
        L.append(f"| {k} | {v} |")
    L += ["", "## 未映射的语用标签（前 60，需裁定）", "", "| 开放标签 | 次数 |", "|---|---:|"]
    for k, v in stats["unmapped_prag_labels"].most_common(60):
        L.append(f"| {k} | {v} |")
    L += ["", "## 未映射的情绪标签", "", "| 开放标签 | 次数 |", "|---|---:|"]
    for k, v in stats["unmapped_emo_labels"].most_common(40):
        L.append(f"| {k} | {v} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--lines", required=True, help="dir holding <segment>.lines.txt from private_redact.py")
    ap.add_argument("--map", default=str(MAP_PATH))
    ap.add_argument("--out", default=None, help="cases json; must be outside the repo")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--context", type=int, default=CONTEXT_LINES)
    args = ap.parse_args()

    ann_root, lines_root = Path(args.annotations), Path(args.lines)
    if is_inside_repo(ann_root) or is_inside_repo(lines_root):
        raise SystemExit("标注与原文目录不能在仓库内。")
    lm = LabelMap(Path(args.map))
    out = ensure_private_out(args.out, "gold/cases-v2.json")
    cases, stats = build_cases(ann_root, lines_root, lm, args.context)
    payload = {
        "schema": "intent-cases-v1",
        "label_map": {"version": lm.version, "digest": lm.digest},
        "source": "external-annotation context-v4 (AI weak gold)",
        "n": len(cases),
        "gold": stats["gold"],
        "weak": stats["weak"],
        "cases": cases,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"cases -> {out}  n={len(cases)} gold={stats['gold']} weak={stats['weak']}")
    if args.report:
        rep = private_eval_dir() / "gold" / f"label-map-report-{date.today().isoformat()}.md"
        rep.parent.mkdir(parents=True, exist_ok=True)
        rep.write_text(render_report(stats, lm, out), encoding="utf-8")
        print(f"report -> {rep}")
    n = max(1, stats["other_total"])
    print(
        f"DONE segments={stats['segments']} other={stats['other_total']} "
        f"prag_unmapped_msgs={stats['prag_unmapped_msgs']} ({stats['prag_unmapped_msgs'] / n:.1%}) "
        f"prag_none_only={stats['prag_none_only_msgs']} ({stats['prag_none_only_msgs'] / n:.1%}) "
        f"emo_none_only={stats['emo_none_only_msgs']} ({stats['emo_none_only_msgs'] / n:.1%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
