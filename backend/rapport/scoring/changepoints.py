"""Exploratory change points over session features (ruptures PELT), for the trend card.

The research interaction study (`rapport.research.interactions`) measured that this project's
sessions can be summarized by five observable features and that PELT finds candidate change
points in them. This module is the production-facing, deliberately conservative wrapper:

- it needs >= 8 usable sessions before it says anything (fewer is noise)
- it reports *stability* (how many cost/penalty settings agree), never a probability
- it says plainly that it is exploratory and has no human labels behind it

Nothing here changes a score; the trend card shows the points as "look here", with the session's
messages as evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime

from rapport.schemas.message_v1 import Conversation, Message, Sender

log = logging.getLogger("rapport.grounded")

GAP_HOURS = 6
MIN_SESSIONS = 8
MAX_POINTS = 4
NOTE = "变化点为探索性结果（不同参数下的一致性），不是概率，也没有人工标注可以给出准确率。"


def _as_dict(msg: Message) -> dict:
    sender = "other" if msg.sender == Sender.other else "me"
    return {
        "id": msg.id,
        "sender": sender,
        "text": msg.text or "",
        "type": msg.type.value if hasattr(msg.type, "value") else str(msg.type),
        "ts": msg.ts.isoformat() if msg.ts else None,
        "ts_precision": msg.ts_precision.value if hasattr(msg.ts_precision, "value") else None,
    }


def detect_change_points(
    conv: Conversation, start: int, end: int, gap_hours: int = GAP_HOURS, max_points: int = MAX_POINTS
) -> dict:
    """Return {"status", "points", "note", "sessions"}; never raises on thin material."""
    try:
        from rapport.research.interactions import detect_changes, session_features
    except Exception:  # noqa: BLE001 - ruptures/numpy missing must not break an analysis
        log.warning("change-point layer unavailable", exc_info=True)
        return {"status": "unavailable", "points": [], "sessions": 0, "note": "变化点依赖（ruptures）不可用。"}

    messages = [_as_dict(m) for m in conv.messages if start <= m.seq <= end]
    ts_by_id = {m["id"]: m["ts"] for m in messages if m["ts"]}
    try:
        rows, coverage = session_features(messages, gap_hours=gap_hours)
    except Exception:  # noqa: BLE001
        log.warning("session features failed", exc_info=True)
        return {"status": "unavailable", "points": [], "sessions": 0, "note": "会话特征计算失败。"}
    if len(rows) < MIN_SESSIONS:
        return {
            "status": "insufficient_sessions",
            "points": [],
            "sessions": len(rows),
            "coverage": coverage,
            "note": f"可用会话 {len(rows)} 段（少于 {MIN_SESSIONS} 段），暂不做变化点检测。",
        }
    try:
        result = detect_changes([row["values"] for row in rows])
    except Exception:  # noqa: BLE001
        log.warning("change-point detection failed", exc_info=True)
        return {"status": "unavailable", "points": [], "sessions": len(rows), "note": "变化点检测失败。"}

    runs = max(1, len(result.get("runs") or []))
    stable = result.get("stable_indices") or []
    points = []
    for index in stable[:max_points]:
        row = rows[index]
        dates = [ts_by_id[i] for i in row["evidence_ids"] if i in ts_by_id]
        points.append({
            "index": index,
            "session_index": row["session_index"],
            "date": min(dates) if dates else None,
            "stability": round(len([r for r in result["runs"] if index in r["indices"]]) / runs, 2),
            "evidence_ids": list(dict.fromkeys(row["evidence_ids"]))[:12],
        })
    return {
        "status": "exploratory",
        "points": points,
        "sessions": len(rows),
        "coverage": coverage,
        "note": NOTE,
        "stability_note": result.get("stability_note"),
    }


def to_schema(payload: dict) -> list:
    """Map the raw dict onto report ChangePoint models (kept here so the schema stays dumb)."""
    from rapport.schemas.report_v1 import ChangePoint

    out = []
    for point in payload.get("points") or []:
        date = point.get("date")
        out.append(
            ChangePoint(
                index=int(point.get("index") or 0),
                date=datetime.fromisoformat(date) if isinstance(date, str) else date,
                stability=float(point.get("stability") or 0.0),
                evidence_ids=list(point.get("evidence_ids") or []),
                note="会话特征（发言占比/轮次/长度/回复间隔/文本占比）出现结构性变化。",
            )
        )
    return out
