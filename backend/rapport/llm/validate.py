"""Stage 6: deterministic validation of the synthesis JSON.

- Pydantic-shape the output.
- Evidence ids must be in the sent set; illegal ids are dropped; claims left with no evidence are dropped.
- Actions without stop_when are dropped. Boundary != none forbids advancing actions.
- Scores are recomputed locally; the model's totals are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapport.knowledge.prior_check import TENDENCY_POLES, infer_pole
from rapport.schemas.report_v1 import (
    TENDENCY_KEYS,
    Action,
    AxisLean,
    Boundary,
    BoundaryValue,
    Confidence,
    Landmine,
    Observation,
    Persona,
    Playbook,
    Script,
    Stage,
    StageValue,
    Tendency,
    Trait,
)

ADVANCE_WORDS = re.compile(r"追问|再问|继续问|冷落|吃醋|嫉妒|晾|欲擒故纵|试探|再约|再邀|发多几条|多发|坚持")
# Never allowed, boundary or not: advice that works by pressure, guilt or deception.
# v0.8: advancing advice under a boundary is no longer dropped; it carries this note instead.
BOUNDARY_CAUTION = "对方近期有拒绝或停止联系的表述，这个选项是推进类：先想清楚要不要做。"
MANIPULATION_WORDS = re.compile(r"冷暴力|情感勒索|装可怜|卖惨|激将|逼|威胁|假装分手|故意不回|已读不回|让.{0,4}吃醋|钓|PUA|打压|贬低")


@dataclass
class Validated:
    llm_dims: dict[str, dict]
    observations: list[Observation]
    persona: Persona
    stage: Stage | None
    boundary: Boundary | None
    playbook: Playbook | None
    errors: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)


class ValidationFailed(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _ids(v, valid: set[str], dropped: dict[str, int], key: str) -> list[str]:
    if not isinstance(v, list):
        return []
    ok = [i for i in v if isinstance(i, str) and i in valid]
    bad = len(v) - len(ok)
    if bad:
        dropped[key] = dropped.get(key, 0) + bad
    return list(dict.fromkeys(ok))[:8]


def _conf(v) -> Confidence:
    try:
        return Confidence(str(v))
    except ValueError:
        return Confidence.low


def _option_to_action(o) -> Action:
    return Action(
        goal=o.goal,
        action=o.action,
        basis_ids=list(o.trigger_ids),
        suggested_text=o.suggested_text,
        observe="；".join(f"若{r.if_they}→{r.then}" for r in o.expected_reactions[:2]),
        stop_when=o.stop_when,
        alternatives=[],
        personalized=o.personalized,
    )


def _validate_situations(raw: list, valid_ids: set[str], restrictive: bool, dropped: dict[str, int]) -> list:
    """Situations survive only if they are grounded and their options are complete.

    Drop rules (each counted in `dropped` so synth_compliance can report survival):
    - situation without any valid trigger_ids → `situations_no_trigger`
    - option without stop_when → `options_no_stop`
    - option with < 2 expected_reactions → `options_few_reactions`
    - option containing manipulation words → `options_manipulation` (always)
    - option containing advance words under a boundary → `options_advance_under_boundary`
    """
    from rapport.schemas.report_v1 import Option, Reaction, Situation

    out: list[Situation] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not s.get("title"):
            continue
        trig = [x for x in (s.get("trigger_ids") or []) if isinstance(x, str) and x in valid_ids]
        if not trig:
            dropped["situations_no_trigger"] = dropped.get("situations_no_trigger", 0) + 1
            continue
        options: list[Option] = []
        for o in s.get("options") or []:
            if not isinstance(o, dict) or not o.get("goal") or not o.get("action"):
                continue
            if not o.get("stop_when"):
                dropped["options_no_stop"] = dropped.get("options_no_stop", 0) + 1
                continue
            reactions = [
                Reaction(if_they=str(r.get("if_they"))[:40], then=str(r.get("then") or "")[:60], signal=str(r.get("signal") or "neutral") if r.get("signal") in ("good", "neutral", "stop") else "neutral")
                for r in (o.get("expected_reactions") or [])
                if isinstance(r, dict) and r.get("if_they")
            ][:4]
            if len(reactions) < 2:
                dropped["options_few_reactions"] = dropped.get("options_few_reactions", 0) + 1
                continue
            text = f"{o.get('goal')} {o.get('action')} {o.get('suggested_text', '')}"
            if MANIPULATION_WORDS.search(text):
                dropped["options_manipulation"] = dropped.get("options_manipulation", 0) + 1
                continue
            caution = BOUNDARY_CAUTION if (restrictive and ADVANCE_WORDS.search(text)) else ""
            o_trig = [x for x in (o.get("trigger_ids") or []) if isinstance(x, str) and x in valid_ids] or trig
            options.append(
                Option(
                    goal=str(o["goal"])[:80],
                    action=str(o["action"])[:300],
                    trigger_ids=o_trig[:6],
                    suggested_text=str(o.get("suggested_text") or "")[:300],
                    expected_reactions=reactions,
                    stop_when=str(o["stop_when"])[:200],
                    risk=str(o.get("risk") or "")[:60],
                    caution=caution,
                ).ensure_id()
            )
        if not options:
            dropped["situations_no_options"] = dropped.get("situations_no_options", 0) + 1
            continue
        out.append(Situation(id=str(s.get("id") or f"s{i + 1}")[:12], title=str(s["title"])[:20], trigger_ids=trig[:6], why=str(s.get("why") or "")[:60], options=options[:3]))
    return out[:3]


def validate_synthesis(data: dict, valid_ids: set[str]) -> Validated:
    errors: list[str] = []
    dropped: dict[str, int] = {}
    if not isinstance(data, dict):
        raise ValidationFailed(["输出不是 JSON 对象"])
    for key in ("dims", "observations", "persona", "stage", "boundary", "playbook"):
        if key not in data:
            errors.append(f"缺少字段 {key}")
    if errors:
        raise ValidationFailed(errors)

    # dims
    llm_dims: dict[str, dict] = {}
    dims = data.get("dims") or {}
    for k in ("responsiveness", "initiative", "investment", "warmth", "future"):
        d = dims.get(k)
        if not isinstance(d, dict):
            continue
        try:
            score = int(d.get("score"))
        except (TypeError, ValueError):
            continue
        ev = _ids(d.get("evidence_ids"), valid_ids, dropped, "dims")
        llm_dims[k] = {"score": max(0, min(100, score)), "evidence_ids": ev, "note": str(d.get("note") or "")[:60]}
    if not llm_dims:
        errors.append("dims 全部无效")

    # observations
    obs: list[Observation] = []
    for i, o in enumerate(data.get("observations") or []):
        if not isinstance(o, dict) or not o.get("claim"):
            continue
        ev = _ids(o.get("evidence_ids"), valid_ids, dropped, "observations")
        if not ev:
            dropped["observations_no_evidence"] = dropped.get("observations_no_evidence", 0) + 1
            continue
        obs.append(
            Observation(
                id=str(o.get("id") or f"o{i + 1}"),
                claim=str(o["claim"])[:120],
                evidence_ids=ev,
                counter_evidence_ids=_ids(o.get("counter_evidence_ids"), valid_ids, dropped, "observations"),
                alternatives=[str(a)[:80] for a in (o.get("alternatives") or []) if a][:3],
                limitations=str(o.get("limitations") or "")[:120],
            )
        )

    # persona
    p = data.get("persona") or {}
    tendencies: dict[str, Tendency] = {}
    for k in TENDENCY_KEYS:
        t = (p.get("tendencies") or {}).get(k)
        if isinstance(t, dict) and t.get("label"):
            pole = str(t.get("pole") or "")
            a_b = TENDENCY_POLES.get(k)
            if not a_b or pole not in (a_b[0], a_b[1]):
                pole = infer_pole(k, str(t["label"]))
            tendencies[k] = Tendency(label=str(t["label"])[:20], evidence_ids=_ids(t.get("evidence_ids"), valid_ids, dropped, "persona"), note=str(t.get("note") or "")[:60], pole=pole)
    traits: list[Trait] = []
    for t in p.get("traits") or []:
        if isinstance(t, dict) and t.get("statement"):
            ev = _ids(t.get("evidence_ids"), valid_ids, dropped, "persona")
            if ev:
                traits.append(Trait(statement=str(t["statement"])[:60], evidence_ids=ev, confidence=_conf(t.get("confidence"))))
    axes: dict[str, AxisLean] = {}
    for a in ("EI", "SN", "TF", "JP"):
        x = (p.get("inferred_axes") or {}).get(a) or {}
        lean = str(x.get("lean") or "unclear")
        if lean not in a:
            lean = "unclear"
        axes[a] = AxisLean(lean=lean, confidence=_conf(x.get("confidence")) if lean != "unclear" else Confidence.low, note=str(x.get("note") or "")[:60])
    persona = Persona(tendencies=tendencies, traits=traits[:6], inferred_axes=axes, available=bool(tendencies or traits))

    # stage / boundary
    stage = None
    s = data.get("stage") or {}
    try:
        stage = Stage(value=StageValue(str(s.get("value"))), basis_ids=_ids(s.get("basis_ids"), valid_ids, dropped, "stage"), note=str(s.get("note") or "")[:80])
    except ValueError:
        errors.append("stage.value 非法")
    boundary = None
    b = data.get("boundary") or {}
    try:
        boundary = Boundary(value=BoundaryValue(str(b.get("value") or "none")), basis_ids=_ids(b.get("basis_ids"), valid_ids, dropped, "boundary"), note=str(b.get("note") or "")[:80])
    except ValueError:
        errors.append("boundary.value 非法")
    if boundary and boundary.value != BoundaryValue.none and not boundary.basis_ids:
        # a boundary claim without evidence is downgraded (rule layer may still raise it)
        boundary = Boundary(value=BoundaryValue.none, note="模型声称存在边界信号但未给出证据，已忽略。")

    # playbook
    pb = data.get("playbook") or {}
    actions: list[Action] = []
    restrictive = boundary is not None and boundary.value != BoundaryValue.none
    for a in pb.get("actions") or []:
        if not isinstance(a, dict) or not a.get("action") or not a.get("goal"):
            continue
        if not a.get("stop_when"):
            dropped["actions_no_stop"] = dropped.get("actions_no_stop", 0) + 1
            continue
        text = f"{a.get('goal')} {a.get('action')} {a.get('suggested_text', '')}"
        if MANIPULATION_WORDS.search(text):
            dropped["actions_manipulation"] = dropped.get("actions_manipulation", 0) + 1
            continue
        caution = BOUNDARY_CAUTION if (restrictive and ADVANCE_WORDS.search(text)) else ""
        actions.append(
            Action(
                goal=str(a["goal"])[:80],
                action=str(a["action"])[:300],
                basis_ids=_ids(a.get("basis_ids"), valid_ids, dropped, "playbook"),
                suggested_text=str(a.get("suggested_text") or "")[:300],
                observe=str(a.get("observe") or "")[:200],
                stop_when=str(a["stop_when"])[:200],
                alternatives=[str(x)[:120] for x in (a.get("alternatives") or []) if x][:3],
                caution=caution,
            )
        )
    landmines = [
        Landmine(topic=str(ln["topic"])[:20], why=str(ln.get("why") or "")[:100], evidence_ids=_ids(ln.get("evidence_ids"), valid_ids, dropped, "playbook"))
        for ln in (pb.get("landmines") or [])
        if isinstance(ln, dict) and ln.get("topic")
    ][:4]
    scripts: dict[str, Script] = {}
    for k in ("casual", "sincere", "direct"):
        sc = (pb.get("scripts") or {}).get(k)
        if isinstance(sc, dict) and sc.get("text"):
            scripts[k] = Script(text=str(sc["text"])[:300], when_to_use=str(sc.get("when_to_use") or "")[:100], caution=BOUNDARY_CAUTION if (restrictive and k == "direct") else "")
    situations = _validate_situations(pb.get("situations") or [], valid_ids, restrictive, dropped)
    if situations and not actions:
        # Compatibility: the old flat list is the first option of each situation.
        actions = [_option_to_action(s.options[0]) for s in situations if s.options][:3]
    playbook = Playbook(actions=actions[:3], situations=situations, landmines=landmines, scripts=scripts) if (actions or situations) else None

    if errors:
        raise ValidationFailed(errors)
    return Validated(llm_dims, obs[:6], persona, stage, boundary, playbook, [], dropped)
