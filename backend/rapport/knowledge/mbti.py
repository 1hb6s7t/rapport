"""MBTI comparison: inferred axes (from chat behaviour) vs declared type. Never decides who is 'right'."""

from __future__ import annotations

from rapport.schemas.report_v1 import AxisLean, Confidence, MbtiComparison

AXES = ("EI", "SN", "TF", "JP")
AXIS_LABEL = {
    "EI": "外向/内向表达倾向",
    "SN": "具体/抽象话题倾向",
    "TF": "逻辑/情感回应倾向",
    "JP": "计划/随性安排倾向",
}


def inferred_string(axes: dict[str, AxisLean]) -> str:
    out = ""
    for a in AXES:
        lean = axes.get(a, AxisLean()).lean
        out += lean if lean in a else "?"
    return out


def compare(declared: str | None, axes: dict[str, AxisLean]) -> MbtiComparison | None:
    if not declared:
        return None
    inferred = inferred_string(axes)
    notes: list[str] = []
    known = 0
    agree = 0
    for i, a in enumerate(AXES):
        lean = axes.get(a, AxisLean())
        if lean.lean not in a or lean.confidence == Confidence.low:
            notes.append(f"{AXIS_LABEL[a]}：材料信息不足，无法比较。")
            continue
        known += 1
        d = declared[i]
        if lean.lean == d:
            agree += 1
            notes.append(f"{AXIS_LABEL[a]}：聊天表现（{lean.lean}）与自述（{d}）相近。")
        else:
            notes.append(
                f"{AXIS_LABEL[a]}：聊天表现偏 {lean.lean}，自述为 {d}。"
                "可能原因：本段材料只覆盖特定情境；对方与你相处时的表达方式不同于自评；模型对短文本的推断本身不稳定。"
            )
    if known == 0:
        verdict = "insufficient"
    elif agree == known:
        verdict = "similar"
    else:
        verdict = "context_differs"
    notes.append("提示：这不是判断谁对谁错，也不是人格测试结果；只用于帮助你理解“对方在和你聊天时是什么样”。")
    return MbtiComparison(declared=declared, inferred=inferred, verdict=verdict, notes=notes)
