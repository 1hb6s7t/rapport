from datetime import UTC, datetime, timedelta

import pytest

from rapport.research.interactions import session_features


def test_session_features_preserve_unknown_time_and_direction():
    base = datetime(2025, 1, 1, tzinfo=UTC)
    rows = [{"id": str(i), "sender": "me" if i % 2 else "other", "type": "text", "text": "合成句子",
             "ts": (base + timedelta(seconds=30*i)).isoformat(), "ts_precision": "second"} for i in range(8)]
    rows.append({"id": "missing", "sender": "other", "type": "text", "text": "未显示时间", "ts": None})
    features, coverage = session_features(rows)
    assert coverage["eligible_messages_with_times"] == 8
    assert features[0]["values"][0] == .5
    assert features[0]["values"][1] == 1
    assert "missing" not in features[0]["evidence_ids"]


def test_upstream_chinese_markers_use_tokens_not_affection_predictions():
    from rapport.research.vendor.convokit_zh.strategy_extractor import (
        get_chinese_politeness_strategy_features,
    )

    features, _ = get_chinese_politeness_strategy_features([[{"tok": "谢谢"}, {"tok": "您"}]])
    assert features["gratitude"] == 1
    assert "affection" not in features


def test_pelt_detects_known_synthetic_step_and_refuses_short_series():
    pytest.importorskip("ruptures")
    from rapport.research.interactions import detect_changes

    assert detect_changes([[0, 0]] * 5)["status"] == "insufficient_sessions"
    result = detect_changes([[0, 0]] * 20 + [[8, 8]] * 20)
    assert 20 in result["stable_indices"]
    assert all(40 not in row["indices"] for row in result["runs"])
