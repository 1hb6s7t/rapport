"""Why the advice changed since the last report (v2 M08).

The requirement is explicit that the playbook is revised, not re-rolled: a user who sees different
advice should be able to read *why* it differs. This compares the previous report's playbook with
the new one and writes short reasons — stage moved, boundary appeared, portrait was revised, the
stated goal changed, or the main action was replaced.

It deliberately does not diff the wording of suggestions: paraphrasing is not a change of advice.
"""

from __future__ import annotations

from rapport.schemas.report_v1 import BOUNDARY_LABELS, STAGE_LABELS, Report


def _stage(value: str | None) -> str:
    return STAGE_LABELS.get(value or "", value or "未知")


def _boundary(value: str | None) -> str:
    return BOUNDARY_LABELS.get(value or "", value or "未知")


def _prev(body: dict | None, *path: str):
    node = body
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def diff_playbook(previous_body: dict | None, report: Report) -> list[str]:
    """Human-readable reasons the current advice differs from the previous report's."""
    if not previous_body:
        return []
    notes: list[str] = []

    prev_stage = _prev(previous_body, "stage", "value")
    if prev_stage and prev_stage != report.stage.value.value:
        moved = "推进" if report.stage.change and report.stage.change.direction == "advance" else (
            "回退" if report.stage.change and report.stage.change.direction == "regress" else "变化"
        )
        notes.append(f"关系阶段从「{_stage(prev_stage)}」{moved}为「{_stage(report.stage.value.value)}」，行动随之调整。")

    prev_boundary = _prev(previous_body, "boundary", "value")
    if prev_boundary and prev_boundary != report.boundary.value.value:
        notes.append(f"边界状态从「{_boundary(prev_boundary)}」变为「{_boundary(report.boundary.value.value)}」，这优先于任何推进建议。")

    prev_version = _prev(previous_body, "persona", "version") or 0
    if report.persona.version and report.persona.version > prev_version:
        reason = report.persona.change_notes[0].reason if report.persona.change_notes else "材料更新"
        notes.append(f"对方画像更新到 v{report.persona.version}（{reason}），侧写与建议据此重算。")

    prev_goals = [a.get("goal") for a in (_prev(previous_body, "playbook", "actions") or []) if isinstance(a, dict)]
    new_goals = [a.goal for a in report.playbook.actions]
    if prev_goals and new_goals and prev_goals[0] != new_goals[0]:
        notes.append(f"主行动从「{prev_goals[0]}」改为「{new_goals[0]}」。")

    prev_landmines = {lm.get("topic") for lm in (_prev(previous_body, "playbook", "landmines") or []) if isinstance(lm, dict)}
    new_landmines = {lm.topic for lm in report.playbook.landmines}
    added = new_landmines - prev_landmines
    if added:
        notes.append("新增雷区：" + "、".join(sorted(added)) + "。")

    return notes[:5]
