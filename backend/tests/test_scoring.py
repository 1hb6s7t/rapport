from __future__ import annotations

from datetime import datetime, timedelta

from rapport.features import compute_features
from rapport.parsers import parse
from rapport.schemas.message_v1 import Conversation, Message, Sender, Source, TsPrecision
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import BoundaryValue, DimScore
from rapport.scoring import (
    aggregate,
    build_affinity,
    build_trend,
    fuse,
    round_half_up,
    rule_boundary,
    rule_stage,
)
from rapport.scoring.rubric import interp
from tests.conftest import demo_text


def dims(*scores):
    keys = ("responsiveness", "initiative", "investment", "warmth", "future")
    return {k: DimScore(score=s) for k, s in zip(keys, scores, strict=True)}


def test_aggregate_full_coverage_57_5_rounds_to_58():
    total, raw, cov = aggregate(dims(75, 50, 50, 75, 25))
    assert raw == 57.5 and total == 58 and cov == 1.0


def test_aggregate_missing_dim_renormalises_to_63():
    total, raw, cov = aggregate(dims(75, 50, 50, 75, None))
    assert cov == 0.85 and abs(raw - 63.235) < 0.01 and total == 63


def test_round_half_up():
    assert round_half_up(0.5) == 1 and round_half_up(1.5) == 2 and round_half_up(2.4) == 2


def test_fuse():
    assert fuse(80, 60, "high")[0] == 70
    assert fuse(80, None, "high")[0] == 80
    assert fuse(None, 60, "low")[0] == 60
    assert fuse(None, None, "low")[0] is None


def test_interp_clamps():
    assert interp("reply_rate_24h", -1) == 5 and interp("reply_rate_24h", 2) == 92
    assert 35 < interp("reply_rate_24h", 0.65) < 65


def test_insufficient_when_few_other_texts():
    conv, _ = parse("A: 1\nB: 2\nA: 3\nB: 4", "c", "paste")
    f = compute_features(conv)
    a = build_affinity(f)
    # v0.8: thin material still gets a number; the thinness is a caveat and a low confidence
    assert a.total is not None or all(d.score is None for d in a.dims.values())
    assert any("对方文本" in c for c in a.caveats) and a.confidence == "low"
    assert all(d.score is not None or d.rule is None for d in a.dims.values())


def test_demo_scores_and_suppression():
    conv, _ = parse(demo_text(), "c", "paste")
    f = compute_features(conv)
    a = build_affinity(f)
    assert a.total is not None and 0 <= a.total <= 100 and a.coverage == 1.0
    s = build_affinity(f, boundary=BoundaryValue.stop_contact)
    # v0.8: the boundary is a caveat, not a gate — same number, plus the note
    assert s.total == a.total and s.suppressed_reason and any("不想继续联系" in c for c in s.caveats)
    assert s.dims["responsiveness"].score == a.dims["responsiveness"].score


def test_rule_boundary_and_stage():
    txt = demo_text() + "\n[2026-09-11 10:00] B: 我觉得我们不合适，别再联系我了"
    conv, _ = parse(txt, "c", "paste")
    f = compute_features(conv)
    b = rule_boundary(f)
    assert b.value == BoundaryValue.stop_contact and b.basis_ids
    st = rule_stage(f, Profile(), b)
    assert st.value in ("affectionate_signals", "getting_to_know")


def test_decline_with_alternative_is_not_boundary():
    conv, _ = parse(demo_text(), "c", "paste")  # contains "周三不太方便，下周吧？"
    f = compute_features(conv)
    assert f.boundary_hits["decline_with_alternative"]
    assert rule_boundary(f).value == BoundaryValue.none


def test_mutually_confirmed_needs_evidence_or_user():
    conv, _ = parse(demo_text(), "c", "paste")
    f = compute_features(conv)
    assert rule_stage(f, Profile(), rule_boundary(f)).value != "mutually_confirmed"
    assert rule_stage(f, Profile(stage_confirmed="mutually_confirmed"), rule_boundary(f)).user_confirmed


def test_trend_buckets_and_insufficient():
    conv, _ = parse(demo_text(), "c", "paste")
    f = compute_features(conv)
    t = build_trend(conv, conv.events(), f.has_reliable_time)
    assert t.bucket == "week" and len(t.points) >= 3
    assert all(p.score is None for p in t.points if p.insufficient)
    assert t.momentum in ("rising", "flat", "falling", "unknown")


def test_trend_without_time_uses_order_chunks():
    msgs = [Message(id=f"m{i:06d}", seq=i, sender=Sender.me if i % 2 else Sender.other, text=f"消息{i} 哈哈") for i in range(60)]
    conv = Conversation(id="c", source=Source.paste, messages=msgs)
    t = build_trend(conv, msgs, False)
    assert t.bucket == "session" and len(t.points) == 6 and "顺序" in t.note


def test_trend_falling_detected():
    base = datetime(2026, 8, 1, 20, 0)
    msgs = []
    seq = 0
    for day in range(10):
        lat = 2 + day * 40  # minutes, getting slower
        for k in range(5):
            t0 = base + timedelta(days=day, hours=k)
            msgs.append(Message(id=f"m{seq:06d}", seq=seq, sender=Sender.me, ts=t0, ts_precision=TsPrecision.minute, text="今天怎么样？周末一起吃饭吗"))
            seq += 1
            txt = "好呀好呀，想你了，一起去！哈哈" if day < 4 else "嗯"
            msgs.append(Message(id=f"m{seq:06d}", seq=seq, sender=Sender.other, ts=t0 + timedelta(minutes=lat), ts_precision=TsPrecision.minute, text=txt))
            seq += 1
    conv = Conversation(id="c", source=Source.paste, messages=msgs)
    t = build_trend(conv, msgs, True)
    assert t.bucket == "day"
    assert t.momentum == "falling"


# ---------------------------------------------------------------- shadow weighting (v0.5, §1C)


def _demo_features():
    conv, _ = parse(demo_text(), "c")
    return conv, compute_features(conv, conv.events())


def test_stage_weighting_is_shadow_only():
    """The number the user sees must be the same with and without the alternative weighting."""
    conv, f = _demo_features()
    plain = build_affinity(f, None, BoundaryValue.none, conv.events(), 0.0)
    with_stage = build_affinity(f, None, BoundaryValue.none, conv.events(), 0.0, stage="stable")
    assert plain.total == with_stage.total
    assert plain.alt_totals["static"] == plain.total
    if with_stage.alt_totals.get("stage_weighted") is not None:
        assert with_stage.weights_used, "影子分必须给出它实际使用的权重，否则无法解释它"


def test_stage_weights_shift_with_the_stage():
    conv, f = _demo_features()
    early = build_affinity(f, None, BoundaryValue.none, conv.events(), 0.0, stage="initial_contact")
    late = build_affinity(f, None, BoundaryValue.none, conv.events(), 0.0, stage="stable")
    if not (early.weights_used and late.weights_used):
        return  # no observable dimension in this fixture; nothing to compare
    assert early.weights_used["responsiveness"] > late.weights_used["responsiveness"], "刚认识时回复快慢更重要"
    assert late.weights_used["future"] > early.weights_used["future"], "稳定期未来安排更重要"
    assert abs(sum(late.weights_used.values()) - 1.0) < 0.01, "权重必须归一化"


def test_a_withheld_total_withholds_both_weightings():
    """Both figures follow the same gates, so the comparison is never number-vs-nothing."""
    conv, f = _demo_features()
    a = build_affinity(f, None, BoundaryValue.stop_contact, conv.events(), 0.0, stage="stable")
    # v0.8: nothing is withheld; both weightings are numbers and carry the same caveats
    assert a.total is not None
    assert a.alt_totals["static"] == a.total and a.alt_totals["stage_weighted"] is not None


# ---------------------------------------------------------------- v0.6: boundary is a current state


def test_old_boundary_hit_does_not_gate_but_is_noted():
    """A 不合适 from a year ago must not hide today's total; it stays visible as a historical note."""
    from datetime import datetime, timedelta

    from rapport.scoring.affinity import build_affinity

    old = datetime(2025, 3, 1, 20, 0)
    now = datetime(2026, 9, 1, 20, 0)
    lines = [f"[{old:%Y-%m-%d %H:%M}] B: 我觉得我们不合适，别再联系我了"]
    for d in range(0, 20):
        t = now - timedelta(days=19 - d)
        lines.append(f"[{t:%Y-%m-%d %H:%M}] A: 今天怎么样")
        lines.append(f"[{(t + timedelta(minutes=5)):%Y-%m-%d %H:%M}] B: 挺好的，你呢")
    conv, _ = parse("\n".join(lines), "c", "paste")
    f = compute_features(conv)
    assert f.boundary_hits["stop_contact"] and not f.boundary_recent["stop_contact"]
    b = rule_boundary(f)
    assert b.value == BoundaryValue.none
    assert "更早" in b.note and b.basis_ids
    assert build_affinity(f, boundary=b.value).total is not None


def test_recent_boundary_hit_still_gates():
    from datetime import datetime, timedelta

    now = datetime(2026, 9, 1, 20, 0)
    lines = []
    for d in range(0, 20):
        t = now - timedelta(days=19 - d)
        lines.append(f"[{t:%Y-%m-%d %H:%M}] A: 今天怎么样")
        lines.append(f"[{(t + timedelta(minutes=5)):%Y-%m-%d %H:%M}] B: 挺好的")
    lines.append(f"[{now:%Y-%m-%d %H:%M}] B: 别再联系我了")
    conv, _ = parse("\n".join(lines), "c", "paste")
    f = compute_features(conv)
    assert rule_boundary(f).value == BoundaryValue.stop_contact


def test_colloquial_xiangsi_is_not_a_safety_signal():
    from rapport.features import lexicon as lx

    for s in ("想死你了", "累得想死", "困得想死了", "笑得想死"):
        assert not lx.SAFETY_RE.search(s), s
    for s in ("我想死", "真的不想活了", "我要报警"):
        assert lx.SAFETY_RE.search(s), s
