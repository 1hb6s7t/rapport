"""Private import check: run every redacted chat export through parse → rules (0 model calls).

This is the first "direction table" on the user's real material: per segment, how many messages
parsed, what share is countable text, how much of the media is unread, the rules-only affinity
total and the boundary status. Output goes to `~/.rapport/eval/` only (`_private.ensure_private_out`).

    cd backend
    uv run python ../scripts/private_import_check.py --material "C:/Users/<you>/.rapport/eval/redacted" \
        --progress "C:/Users/<you>/.rapport/eval/import-check.progress"

No chat text is printed or written: only counts, scores and warning codes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, is_inside_repo  # noqa: E402
from _probe import Probe  # noqa: E402

from rapport.features import compute_features  # noqa: E402
from rapport.features.sessions import Session, split_sessions  # noqa: E402
from rapport.jobs.pipeline import run_rules  # noqa: E402
from rapport.parsers import ParseError, ParseOptions, parse  # noqa: E402
from rapport.schemas.message_v1 import MEDIA_TYPES, MsgType  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402
from rapport.scoring.evidence import evidence_breakdown, n_eff_from_events  # noqa: E402


class _Row:
    """Minimal event row so the ledger math can run without a database."""

    def __init__(self, i: int, m, episode: str):
        self.fingerprint = f"e{i}"
        self.episode_id = episode
        self.type = m.type.value
        self.ts_precision = m.ts_precision.value
        self.origin = "observed_chat"
        self.confirmed = True


def _ledger_rows(conv, events, gap_hours: float = 6.0) -> list[_Row]:
    """Episode = session from the feature layer, which is what the import path stores too."""
    has_time = any(m.has_reliable_ts for m in events)
    sessions = split_sessions(events, gap_hours) if has_time else [Session(0, events)]
    session_of: dict[str, str] = {}
    for s in sessions:
        for m in s.messages:
            session_of[m.id] = f"s{s.index}"
    return [_Row(i, m, session_of.get(m.id, "s?")) for i, m in enumerate(events)]


def check_one(path: Path, probe: Probe, goal: str) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        conv, parser_id = parse(text, path.stem, None, path.name, ParseOptions())
    except ParseError as e:
        probe.line(f"FAIL {path.name} parse_error={e.code}")
        return {"segment": path.stem, "error": e.code}
    msgs = conv.messages
    events = conv.events()
    text_events = [m for m in events if m.is_text]
    media_events = [m for m in events if m.type in MEDIA_TYPES]
    from_voice = sum(1 for m in events if m.meta.get("from_voice"))
    unknown = sum(1 for m in events if m.type == MsgType.unknown)
    profile = Profile(me=Person(alias="我"), other=Person(alias="对方"), goal=goal)
    rows = _ledger_rows(conv, events)
    n_eff = n_eff_from_events(rows)
    bd = evidence_breakdown(rows)
    rep = run_rules(conv, events, profile, n_eff=n_eff, evidence_counts=bd)
    f = compute_features(conv, events)
    first_ts = next((m.ts for m in msgs if m.ts), None)
    last_ts = next((m.ts for m in reversed(msgs) if m.ts), None)
    dims = {k: v.rule for k, v in rep.affinity.dims.items()}
    row = {
        "segment": path.stem,
        "parser": parser_id,
        "messages": len(msgs),
        "events": len(events),
        "text": len(text_events),
        "text_share": round(len(text_events) / max(1, len(events)), 3),
        "from_voice": from_voice,
        "media_unread": len(media_events),
        "unknown": unknown,
        "span_days": (last_ts - first_ts).days + 1 if first_ts and last_ts else None,
        "sessions": f.n_sessions,
        "n_eff": round(n_eff, 1),
        "evidence_counts": bd,
        "coverage": rep.affinity.coverage,
        "total": rep.affinity.total,
        "dims": dims,
        "stage": rep.stage.value,
        "boundary": rep.boundary.value,
        "warnings": sorted({w.code for w in conv.warnings}),
    }
    probe.line(
        f"DONE {path.name} msgs={row['messages']} text={row['text']} ({row['text_share']:.0%}) voice_txt={from_voice} "
        f"media_unread={row['media_unread']} n_eff={row['n_eff']} total={row['total']} stage={row['stage']} boundary={row['boundary']}"
    )
    return row


def render(rows: list[dict], material: Path) -> str:
    out = [
        "# 真实材料导入冒烟（规则模式，0 次模型调用）",
        "",
        f"- 日期：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 材料目录：{'（仓库外）' if not is_inside_repo(material) else material}",
        f"- 段数：{len(rows)}；失败：{sum(1 for r in rows if 'error' in r)}",
        "- 口径：规则层分数是行为统计，不是好感度预测；`n_eff` 只计已读文本（含微信转写语音），未读媒体为 0。",
        "",
        "| 段 | 消息 | 文本 | 文本占比 | 转写语音 | 未读媒体 | 跨度(天) | 会话 | n_eff | 覆盖 | 总分 | 阶段 | 边界 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        if "error" in r:
            out.append(f"| {r['segment']} | 解析失败 `{r['error']}` |  |  |  |  |  |  |  |  |  |  |  |")
            continue
        out.append(
            f"| {r['segment']} | {r['messages']} | {r['text']} | {r['text_share']:.0%} | {r['from_voice']} | {r['media_unread']} | "
            f"{r['span_days'] if r['span_days'] is not None else '—'} | {r['sessions']} | {r['n_eff']} | {r['coverage']} | "
            f"{r['total'] if r['total'] is not None else '未出分'} | {r['stage']} | {r['boundary']} |"
        )
    out += ["", "## 五维规则分", "", "| 段 | " + " | ".join(next((list(r["dims"]) for r in rows if "dims" in r), [])) + " |"]
    keys = next((list(r["dims"]) for r in rows if "dims" in r), [])
    out.append("|---|" + "---:|" * len(keys))
    for r in rows:
        if "dims" in r:
            out.append(f"| {r['segment']} | " + " | ".join("—" if r["dims"].get(k) is None else str(r["dims"][k]) for k in keys) + " |")
    out += ["", "## 解析警告", ""]
    for r in rows:
        if "warnings" in r:
            out.append(f"- {r['segment']}: {', '.join(r['warnings']) or '无'}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material", required=True, help="directory of redacted single-chat JSON exports (outside the repo)")
    ap.add_argument("--glob", default="*.json")
    ap.add_argument("--goal", default="invite")
    ap.add_argument("--out", default=None)
    ap.add_argument("--progress", default=None)
    args = ap.parse_args()

    material = Path(args.material)
    if is_inside_repo(material):
        raise SystemExit("材料目录不能在仓库内。")
    out = ensure_private_out(args.out, f"import-check-{datetime.now():%Y-%m-%d}.md")
    files = sorted(p for p in material.glob(args.glob) if not p.name.startswith("_"))
    if not files:
        raise SystemExit(f"{material} 下没有匹配 {args.glob} 的文件")
    probe = Probe(args.progress)
    probe.line(f"START segments={len(files)} out={out}")
    rows = [check_one(p, probe, args.goal) for p in files]
    out.write_text(render(rows, material), encoding="utf-8")
    probe.done(f"DONE segments={len(rows)} failed={sum(1 for r in rows if 'error' in r)} report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
