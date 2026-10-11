"""Outcome direction table (v0.5 plan §5.2, stage 6): do the dimensions move the way the user
later said the relationship went? A table for a human to read — nothing here fits anything.

For each relationship with >= 2 answered check-ins: pair each outcome with the two snapshots
that bracket it (the report it was asked about and the previous comparable one), compute the
per-dimension delta, and count how often the sign of the delta agrees with the outcome
(better -> +, worse/ended -> -, same -> |delta| <= tolerance). Output goes to ~/.rapport/eval/.

    cd backend
    uv run python ../scripts/outcome_direction.py            # reads the app database
    uv run python ../scripts/outcome_direction.py --db "C:/path/rapport.db"

Decision rule the plan wrote down: only if >= 3 dimensions agree with the outcome in >= 3
relationships × 2 check-ins does STAGE_BETA get switched into the main total. That decision is
made by a person reading this table, not by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out  # noqa: E402

from rapport.config import get_settings  # noqa: E402
from rapport.db import Database, Outcome, Snapshot  # noqa: E402
from rapport.schemas.report_v1 import DIMENSIONS  # noqa: E402

SIGN = {"better": 1, "worse": -1, "ended": -1, "same": 0, "unknown": None}
TOL = 3  # |delta| <= TOL counts as "same"


def _dim_score(dims: dict, key: str) -> float | None:
    d = (dims or {}).get(key) or {}
    v = d.get("score") if isinstance(d, dict) else d
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_rows(db: Database) -> list[dict]:
    rows: list[dict] = []
    with db.session() as s:
        outcomes = s.query(Outcome).filter(Outcome.answered_at.isnot(None), Outcome.outcome != "unknown").order_by(Outcome.answered_at).all()
        for o in outcomes:
            snaps = (
                s.query(Snapshot)
                .filter(Snapshot.relationship_id == o.relationship_id)
                .order_by(Snapshot.created_at)
                .all()
            )
            if not snaps:
                continue
            # the snapshot of the report the check-in was about, and the previous comparable one
            idx = next((i for i, sn in enumerate(snaps) if sn.report_id == o.report_id), None)
            if idx is None or idx == 0:
                continue
            cur, prev = snaps[idx], snaps[idx - 1]
            comparable = (cur.rubric_version, cur.features_version, cur.provider, cur.model) == (prev.rubric_version, prev.features_version, prev.provider, prev.model)
            deltas = {}
            for k in DIMENSIONS:
                a, b = _dim_score(prev.dims, k), _dim_score(cur.dims, k)
                deltas[k] = None if a is None or b is None else round(b - a, 1)
            total_delta = None if cur.total is None or prev.total is None else cur.total - prev.total
            rows.append(
                {
                    "relationship_id": o.relationship_id,
                    "report_id": o.report_id,
                    "answered_at": o.answered_at.isoformat(timespec="minutes"),
                    "outcome": o.outcome,
                    "met_since": bool(o.met_since),
                    "comparable": comparable,
                    "total_delta": total_delta,
                    "deltas": deltas,
                }
            )
    return rows


def agreement(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Per dimension: agree / disagree / undecided counts across comparable rows."""
    out: dict[str, dict[str, int]] = {k: {"agree": 0, "disagree": 0, "undecided": 0} for k in list(DIMENSIONS) + ["total"]}
    for r in rows:
        if not r["comparable"]:
            continue
        sign = SIGN.get(r["outcome"])
        if sign is None:
            continue
        for k in list(DIMENSIONS) + ["total"]:
            d = r["total_delta"] if k == "total" else r["deltas"].get(k)
            if d is None:
                out[k]["undecided"] += 1
                continue
            if sign == 0:
                out[k]["agree" if abs(d) <= TOL else "disagree"] += 1
            elif (d > TOL and sign > 0) or (d < -TOL and sign < 0):
                out[k]["agree"] += 1
            elif abs(d) <= TOL:
                out[k]["undecided"] += 1
            else:
                out[k]["disagree"] += 1
    return out


def render(rows: list[dict], agree: dict) -> str:
    rels = defaultdict(int)
    for r in rows:
        rels[r["relationship_id"]] += 1
    L = [
        "# 回访方向表（阶段 6）",
        "",
        f"- 日期：{date.today().isoformat()}",
        f"- 已回答的回访：{len(rows)} 次，覆盖 {len(rels)} 段关系（各 {dict(rels)}）",
        f"- 可比对（同规则/特征/模型版本）：{sum(1 for r in rows if r['comparable'])} 次",
        f"- 口径：Δ 是本次报告相对上一次快照的分数变化；「同向」= 回访说变好且 Δ > {TOL}，或说变差/结束且 Δ < −{TOL}，或说没变且 |Δ| ≤ {TOL}。**这是方向计数，不是相关系数，也不是校准。**",
        "- 决策规则（计划 §5.2 / §6）：≥3 段关系 × 2 次回访，且 ≥3 个维度同向多于反向，才由人决定把 STAGE_BETA 切进主分。样本不足则静态权重继续作主分。",
        "",
        "## 各维同向计数",
        "",
        "| 维度 | 同向 | 反向 | 不能判 |",
        "|---|---:|---:|---:|",
    ]
    for k, c in agree.items():
        L.append(f"| {k} | {c['agree']} | {c['disagree']} | {c['undecided']} |")
    enough = len(rels) >= 3 and all(n >= 2 for n in rels.values())
    aligned = sum(1 for k in DIMENSIONS if agree[k]["agree"] > agree[k]["disagree"])
    L += [
        "",
        "## 判读",
        "",
        f"- 样本门（≥3 段 × 2 次）：{'满足' if enough else '不满足'}。",
        f"- 同向多于反向的维度数：{aligned}/5（门 ≥3）。",
        "- 结论由人写；本表不做任何拟合。" + ("" if enough and aligned >= 3 else " 目前证据不足以切换主分。"),
        "",
        "## 逐次",
        "",
        "| 关系 | 回访 | 结果 | 见过面 | 可比 | Δ总分 | " + " | ".join(f"Δ{k}" for k in DIMENSIONS) + " |",
        "|---|---|---|---|---|---:|" + "---:|" * len(DIMENSIONS),
    ]
    for r in rows:
        L.append(
            f"| {r['relationship_id']} | {r['answered_at']} | {r['outcome']} | {'是' if r['met_since'] else ''} | {'是' if r['comparable'] else '否'} | "
            f"{'—' if r['total_delta'] is None else r['total_delta']} | "
            + " | ".join("—" if r["deltas"].get(k) is None else str(r["deltas"][k]) for k in DIMENSIONS)
            + " |"
        )
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="path to rapport.db (default: the app's data dir)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    db_path = Path(args.db) if args.db else get_settings().db_path
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    db = Database(db_path)
    rows = build_rows(db)
    agree = agreement(rows)
    out = ensure_private_out(args.out, f"outcome-direction-{date.today().isoformat()}.md")
    out.write_text(render(rows, agree), encoding="utf-8")
    (out.with_suffix(".json")).write_text(json.dumps({"rows": rows, "agreement": agree}, ensure_ascii=False, indent=1), encoding="utf-8")
    rels = {r["relationship_id"] for r in rows}
    print(f"DONE outcomes={len(rows)} relationships={len(rels)} comparable={sum(1 for r in rows if r['comparable'])} report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
