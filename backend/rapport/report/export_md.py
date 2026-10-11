"""Markdown export. Default: no raw evidence text (ids only); optional minimal snippets."""

from __future__ import annotations

from rapport.schemas.report_v1 import BOUNDARY_LABELS, DIM_LABELS, DIMENSIONS, STAGE_LABELS, Report

MOMENTUM_ZH = {"rising": "上升", "flat": "平稳", "falling": "下降", "unknown": "样本不足"}
MODE_ZH = {"demo": "演示（合成数据）", "rules": "规则模式（未使用 AI）", "byok": "AI 分析（自带 Key）"}


def _v(x):
    return getattr(x, "value", x)


def _ids(ids: list[str]) -> str:
    return f"（证据：{', '.join(ids)}）" if ids else ""


OBSERVED_MD = {"accepted": "接受了", "alt_time": "改了时间", "declined": "拒绝了", "no_reply": "没有回应", "other": "反应不明确"}


def to_markdown(r: Report, include_evidence: bool = False) -> str:
    L: list[str] = []
    L.append(f"# 心迹 · 关系复盘报告 `{r.id}`")
    L.append("")
    L.append(f"- 模式：{MODE_ZH.get(_v(r.mode), _v(r.mode))}" + ("（合成演示数据）" if r.synthetic else ""))
    L.append(f"- 生成时间：{r.created_at:%Y-%m-%d %H:%M}")
    L.append(f"- 版本：app {r.versions.app} · {r.versions.rubric} · {r.versions.features}" + (f" · {r.versions.provider}/{r.versions.model}" if r.versions.model else ""))
    L.append("")
    L.append("> 好感度指数不是概率。" + r.affinity.disclaimer)
    L.append("")
    s = r.scope
    L.append("## 1. 分析范围与材料质量")
    L.append(f"- 消息总数 {s.msg_total}，纳入分析 {s.msg_analyzed}；对方文本 {s.text_other} 条，我方文本 {s.text_me} 条；会话 {s.sessions} 段")
    if s.range_start and s.range_end:
        L.append(f"- 时间范围：{s.range_start:%Y-%m-%d} ~ {s.range_end:%Y-%m-%d}（{'有' if s.has_reliable_time else '无'}可靠时间）")
    if s.quality_flags:
        L.append(f"- 质量提示：{', '.join(s.quality_flags)}")
    L.append("")
    a = r.affinity
    L.append("## 2. 好感度指数")
    if a.total is not None:
        L.append(f"**总分 {a.total} / 100**（观察覆盖度 {a.coverage:.2f}，置信度 {a.confidence}）")
    else:
        L.append("**总分不可用**：没有任何可用维度")
    for cv in a.caveats:
        L.append(f"- ⚠ {cv}")
    L.append("")
    L.append("| 维度 | 分数 | 规则 | AI | 置信度 | 说明 |")
    L.append("|---|---|---|---|---|---|")
    for k in DIMENSIONS:
        d = a.dims.get(k)
        if not d:
            continue
        f = lambda x: "—" if x is None else str(x)  # noqa: E731
        L.append(f"| {DIM_LABELS[k]} | {f(d.score)} | {f(d.rule)} | {f(d.llm)} | {_v(d.confidence)} | {d.note} |")
    L.append("")
    L.append(f"### 趋势（{r.trend.bucket}，动量：{MOMENTUM_ZH[r.trend.momentum]}）")
    L.append("| 分段 | 分数 | 消息数 |")
    L.append("|---|---|---|")
    for p in r.trend.points:
        L.append(f"| {p.label} | {'样本不足' if p.insufficient else p.score} | {p.n_msgs} |")
    if r.trend.note:
        L.append(f"\n{r.trend.note}")
    L.append("")
    if r.intent_readings:
        L.append("## 3. 语义与情绪（逐句）")
        L.append("")
        L.append("_每条都基于上下文判断，允许同时成立多个标签；证据不足时保留候选解释而不下结论。_")
        L.append("")
        for ir in r.intent_readings:
            who = "我" if ir.speaker == "me" else "对方"
            tags = "、".join(ir.pragmatic_tags) or "—"
            emo = "、".join(ir.emotions) or "—"
            L.append(f"- **{who}：{ir.surface_text}** {_ids(ir.evidence_ids)}")
            L.append(f"  - 语用：{tags}；情绪：{emo}；字面：{ir.literal_affect or '—'}")
            if ir.ambiguous:
                L.append(f"  - 材料不足：{ir.abstain_reason or '上下文不足以判断'}")
            if ir.suppressed_reason:
                L.append(f"  - 已降级：{ir.suppressed_reason}")
            for c in ir.candidates:
                L.append(f"  - 可能解释（{c.likelihood.value if hasattr(c.likelihood, 'value') else c.likelihood}）：{c.reading}")
        L.append("")
    if r.observations:
        L.append("## 4. 观察与证据")
        for o in r.observations:
            L.append(f"- **{o.claim}** {_ids(o.evidence_ids)}")
            if o.counter_evidence_ids:
                L.append(f"  - 反证：{', '.join(o.counter_evidence_ids)}")
            for alt in o.alternatives:
                L.append(f"  - 其它解释：{alt}")
            if o.limitations:
                L.append(f"  - 局限：{o.limitations}")
        L.append("")
    p = r.persona
    L.append("## 5. 对方的沟通侧写")
    if not p.available:
        L.append("信息不足或未开启 AI 分析。")
    else:
        L.append(f"_{p.limitations}_")
        for k, t in p.tendencies.items():
            from rapport.schemas.report_v1 import TENDENCY_LABELS

            L.append(f"- {TENDENCY_LABELS.get(k, k)}：{t.label} {_ids(t.evidence_ids)}")
        for t in p.traits:
            L.append(f"- 特质：{t.statement}（{_v(t.confidence)}）{_ids(t.evidence_ids)}")
        axes = " ".join(f"{k}:{v.lean}({_v(v.confidence)})" for k, v in p.inferred_axes.items())
        L.append(f"- 四轴倾向（仅本段材料）：{axes}")
        if p.mbti_comparison:
            c = p.mbti_comparison
            L.append(f"- 与自述 MBTI {c.declared} 对照：{c.verdict}（推断 {c.inferred}）")
            for n in c.notes:
                L.append(f"  - {n}")
    L.append("")
    L.append("## 6. 关系阶段与边界")
    L.append(f"- 阶段：{STAGE_LABELS[_v(r.stage.value)]}" + ("（用户确认）" if r.stage.user_confirmed else "") + f" {_ids(r.stage.basis_ids)} {r.stage.note}")
    L.append(f"- 边界：{BOUNDARY_LABELS[_v(r.boundary.value)]} {_ids(r.boundary.basis_ids)} {r.boundary.note}")
    L.append("")
    L.append("## 7. 攻略")
    if r.playbook.change_notes:
        L.append("")
        L.append("_与上次相比的变化：_")
        for n in r.playbook.change_notes:
            L.append(f"- {n}")
    if r.playbook.note:
        L.append(f"_{r.playbook.note}_")
    if r.playbook.done_review:
        L.append("")
        L.append("### 上次「我做了」的回顾")
        for d in r.playbook.done_review:
            L.append(f"- {d.goal}：对方{OBSERVED_MD.get(d.observed, d.observed)}。{d.verdict}")
    for s_ in r.playbook.situations:
        L.append("")
        L.append(f"### 情境：{s_.title}")
        if s_.why:
            L.append(f"_{s_.why}_")
        for j, o in enumerate(s_.options, 1):
            L.append(f"- 选项 {j}：**{o.goal}** — {o.action}")
            if o.suggested_text:
                L.append(f"  - 示例：{o.suggested_text}")
            for rx in o.expected_reactions:
                L.append(f"  - 若对方{rx.if_they} → {rx.then}")
            L.append(f"  - 停止条件：{o.stop_when}")
            if o.risk:
                L.append(f"  - 风险：{o.risk}")
    for i, act in enumerate(r.playbook.actions, 1):
        L.append(f"### 行动 {i}：{act.goal}" + ("" if act.personalized else "（通用骨架）"))
        L.append(f"- 做什么：{act.action} {_ids(act.basis_ids)}")
        if act.suggested_text:
            L.append(f"- 示例：「{act.suggested_text}」")
        if act.observe:
            L.append(f"- 观察：{act.observe}")
        L.append(f"- 停止条件：{act.stop_when}")
        for alt in act.alternatives:
            L.append(f"- 备选：{alt}")
    if r.playbook.landmines:
        L.append("### 雷区")
        for lm in r.playbook.landmines:
            L.append(f"- {lm.topic}：{lm.why} {_ids(lm.evidence_ids)}")
    if r.playbook.scripts:
        L.append("### 话术模板（示例，不保证结果）")
        for k, sc in r.playbook.scripts.items():
            L.append(f"- {k}：「{sc.text}」 — {sc.when_to_use}")
    L.append("")
    if r.fun.zodiac_pair or r.fun.chinese_zodiac_pair:
        L.append("## 8. 娱乐彩蛋（权重 0）")
        for item in (r.fun.zodiac_pair, r.fun.chinese_zodiac_pair):
            if item:
                L.append(f"- {item.title}：{item.text}")
        L.append(f"_{r.fun.disclaimer}_")
        L.append("")
    if include_evidence and r.evidence:
        L.append("## 附录：被引用的消息片段（已脱敏）")
        for i, e in sorted(r.evidence.items()):
            who = "A" if e.sender == "me" else "B" if e.sender == "other" else e.sender
            ts = f" {e.ts:%m-%d %H:%M}" if e.ts else ""
            L.append(f"- `{i}`{ts} {who}: {e.text}")
        L.append("")
    if r.cost.actual_usage:
        u = r.cost.actual_usage
        L.append(f"_用量：{u.calls} 次调用，输入 {u.prompt_tokens} / 输出 {u.completion_tokens} tokens" + (f"，约 {r.cost.actual_cny} 元_" if r.cost.actual_cny is not None else "_"))
    L.append("")
    L.append("_由 Rapport（心迹）生成。AI 输出可能有误；本报告不构成心理评估或行为指令。_")
    return "\n".join(L)
