"""Check an AI-produced annotation file *before* you spend an evening reviewing it by hand.

The annotation prompt (`docs/ANNOTATION-PROMPT.md`) asks an external model for strict JSON against
a fixed label list. In practice a model will still invent a tag, drop a field, or paraphrase the
original line — and finding that out after you have corrected 400 entries is expensive. This walks
the file first and reports, in order: structural problems, labels outside the fixed lists, lines
that cannot be found in the material, and the entries the annotator itself flagged as uncertain.

Nothing here judges whether a label is *right* — only whether it is readable and traceable. The
review list it writes is for the part only you can do.

Usage:
    uv run python ../scripts/annotation_check.py --labels "C:/path/annotated.json" --material "C:/path/chat.txt"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out  # noqa: E402

from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS, BoundaryValue  # noqa: E402

CONFIDENCE = ("high", "medium", "low")
BOUNDARIES = tuple(v.value for v in BoundaryValue)
SPEAKERS = ("me", "other")


def _material_index(text: str) -> tuple[set[str], set[str]]:
    """Two ways a quoted line can match the material: the whole line, or the message body.

    The material is `[timestamp] speaker: text` in some exports and something else in others, and
    the annotator quotes the *text*. Taking every position after a colon as a candidate body is
    loose on purpose — the question here is only "can this be traced to the material", not "parse
    this format correctly".
    """
    lines = {ln.strip() for ln in text.splitlines() if ln.strip()}
    bodies: set[str] = set()
    for ln in lines:
        for m in re.finditer(r"[:：]\s*", ln):
            body = ln[m.end() :].strip()
            if body:
                bodies.add(body)
    return lines, bodies


def _check_entry(i: int, e: dict, material_lines: set[str], material_bodies: set[str], material_text: str, problems: list[str]) -> dict | None:
    where = f"messages[{i}]"
    if not isinstance(e, dict):
        problems.append(f"{where}: 不是对象")
        return None
    speaker = e.get("speaker")
    if speaker not in SPEAKERS:
        problems.append(f"{where}: speaker={speaker!r} 不在 {SPEAKERS}")
    text = (e.get("text") or "").strip()
    if not text:
        problems.append(f"{where}: text 为空")
    elif material_lines and text not in material_lines and text not in material_bodies:
        if text in material_text:
            problems.append(f"{where}: 原文只在材料里跨行出现（可能合并了相邻消息）：{text[:30]}…")
        else:
            problems.append(f"{where}: 原文在材料里找不到（模型改写过？）：{text[:30]}…")
    for field, allowed in (("pragmatic_tags", PRAGMATIC_TAGS), ("emotions", EMOTIONS)):
        vals = e.get(field)
        if vals is None:
            problems.append(f"{where}: 缺字段 {field}")
            continue
        if not isinstance(vals, list):
            problems.append(f"{where}: {field} 不是列表")
            continue
        for v in vals:
            if v not in allowed:
                problems.append(f"{where}: {field} 出现词表外的标签 {v!r}")
    boundary = e.get("boundary", "none")
    if boundary not in BOUNDARIES:
        problems.append(f"{where}: boundary={boundary!r} 不在 {BOUNDARIES}")
    conf = e.get("confidence")
    if conf is not None and conf not in CONFIDENCE:
        problems.append(f"{where}: confidence={conf!r} 不在 {CONFIDENCE}")
    if "needs_review" in e and not isinstance(e["needs_review"], bool):
        problems.append(f"{where}: needs_review 不是布尔值")
    return e


def _needs_review(e: dict) -> bool:
    """What you actually have to look at: the annotator's own doubts, plus anything low-confidence."""
    return bool(e.get("needs_review")) or (e.get("confidence") or "low") != "high"


def _review_markdown(entries: list[dict], label_path: Path, annotator: str) -> str:
    L = [
        "# 需要你复核的标注条目",
        "",
        f"- 来源：`{label_path.name}`（本文件不复制整段聊天，只列这些条目）",
        f"- 标注者：{annotator or '未署名'}",
        f"- 条目数：{len(entries)}（AI 自己标了不确定，或置信度不是 high 的）",
        "",
        "复核方法：对的就在「你的判断」里写 √；错的写下正确标签（必须取自固定词表）。改完后把纠正写回 JSON。",
        "",
        "| # | 对方说的是 | AI 的标签 | 依据 | 置信度 | 你的判断 |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        tags = "、".join(e.get("pragmatic_tags") or []) or "—"
        emo = "、".join(e.get("emotions") or [])
        basis = "；".join(e.get("evidence") or [])[:60] or "—"
        L.append(f"| {e.get('index', '')} | {e.get('text', '')[:40]} | {tags}{(' / ' + emo) if emo else ''} | {basis} | {e.get('confidence', '')} |  |")
    L += [
        "",
        "复核完之后：把您改过的条目在原 JSON 里改 `pragmatic_tags`，并加 `\"corrected_by\": \"user\"`。"
        "只有改过的条目能进金标准；没改的留在弱金里，只用来找明显分歧。",
    ]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True, help="the JSON the external model produced")
    ap.add_argument("--material", default=None, help="the original chat file, to check quotes against it")
    ap.add_argument("--out", default=None, help="where to write the review list (private dir by default)")
    args = ap.parse_args()

    path = Path(args.labels)
    if not path.exists():
        print(f"找不到标注文件：{path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"不是合法 JSON：{e}", file=sys.stderr)
        print("最常见的原因：模型把 JSON 包在解释文字里，或被截断。让它只输出 JSON 再存一次。", file=sys.stderr)
        return 2

    entries = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        print("顶层缺少 messages 列表（提示词要求的字段）。", file=sys.stderr)
        return 2

    lines: set[str] = set()
    bodies: set[str] = set()
    text = ""
    if args.material:
        mp = Path(args.material)
        if not mp.exists():
            print(f"找不到材料文件：{mp}", file=sys.stderr)
            return 2
        text = mp.read_text(encoding="utf-8")
        lines, bodies = _material_index(text)

    problems: list[str] = []
    good: list[dict] = []
    for i, e in enumerate(entries):
        if _check_entry(i, e, lines, bodies, text, problems) is not None:
            good.append(e)

    others = [e for e in good if e.get("speaker") == "other"]
    tag_counts = Counter(t for e in others for t in (e.get("pragmatic_tags") or []))
    boundary_counts = Counter(e.get("boundary", "none") for e in others)
    review = [e for e in others if _needs_review(e)]

    print(f"文件：{path.name}  标注者：{data.get('annotator', '未署名') if isinstance(data, dict) else '未署名'}")
    print(f"条目 {len(entries)} 条（对方 {len(others)} 条，我方 {len(good) - len(others)} 条）")
    print(f"格式问题 {len(problems)} 处" + ("：" if problems else "（无）"))
    for p in problems[:40]:
        print("  - " + p)
    if len(problems) > 40:
        print(f"  …… 还有 {len(problems) - 40} 处")
    print("标签分布：" + ("、".join(f"{k}×{v}" for k, v in tag_counts.most_common()) or "（空）"))
    print("边界分布：" + "、".join(f"{k}×{v}" for k, v in boundary_counts.most_common()))
    print(f"需要你复核：{len(review)} 条" + (f"（占对方条目的 {len(review) / len(others):.0%}）" if others else ""))

    if review:
        out = ensure_private_out(args.out, f"{path.stem}-review.md")
        out.write_text(_review_markdown(review, path, data.get("annotator", "")), encoding="utf-8")
        print(f"复核清单写入 {out}")
    print(f"DONE labels={len(entries)} other={len(others)} problems={len(problems)} review={len(review)}", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
