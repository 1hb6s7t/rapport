"""The intent harness's own reporting discipline (docs/EVAL.md §14).

These pin the parts of `scripts/intent_eval.py` a reader relies on to trust a number: the case
count and the slice in the header, the failure tally, and the per-case table that makes a single
judgement checkable — including in `--repeat` mode, which used to print a summary table only, and
including a run of a single condition, whose per-tag table used to be silently empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import intent_eval as ie  # noqa: E402  (the script inserts its own backend path on import)


@pytest.fixture(scope="module")
def harness():
    from rapport.llm.fake import FakeProvider

    return FakeProvider(), ie.load_library()


@pytest.fixture(scope="module")
def rows(harness):
    provider, library = harness
    return [ie.evaluate_case(provider, 1, ie.PROBES[0], "single", library)]


def test_success_and_failure_rows_carry_the_same_keys(harness, rows):
    provider, _library = harness
    good = rows[0]
    assert set(good) == set(ie.ROW_FIELDS)
    bad = ie._error_row(1, ie.PROBES[0], "single", "boom")
    # `error` is the one key only a failure row adds; nothing else may differ, or one of the two
    # paths would render a row with a missing column.
    assert set(bad) - set(ie.ROW_FIELDS) == {"error"}
    assert set(ie.ROW_FIELDS) - set(bad) == set()


def test_rows_export_the_basis_for_each_judgement(rows):
    good = rows[0]
    # What makes a wrong call correctable rather than merely countable.
    for key in ("candidates", "tag_basis", "counter_evidence_ids", "evidence_ids", "evidence_strength", "units"):
        assert key in good, key


def test_repeat_report_prints_per_case_tables(rows):
    per_rep = {"context": [ie.score(rows)]}
    md = ie.render_repeat_markdown("m", per_rep, {"context#rep1": rows}, cases=1, slice_note="--limit 1")
    assert "## 逐条（context#rep1）" in md
    assert "## 判断依据与候选解释（context#rep1）" in md
    assert "--limit 1" in md
    # The abstain rate is printed by the console summary, so the report must carry it too.
    assert "abstain_rate" in md


def test_slice_line_reports_the_run_not_the_pool():
    line = ie._slice_line(7)
    assert "场景数：7" in line
    # The pool size is context for the reader, never the number presented as this run's size.
    assert f"题池共 {len(ie.PROBES)} 条" in line
    assert line.index("场景数：7") < line.index("题池共")


def test_per_tag_table_falls_back_to_the_condition_that_ran(rows):
    md = ie.render_markdown("m", {"single": ie.score(rows)}, {"single": rows}, cases=1)
    assert "（按 `single` 条件展开）" in md
    # A heading with no rows under it is the failure mode this replaced.
    assert "| 标签 | 原始 P |" in md
    assert md.index("| 标签 | 原始 P |") < md.index("## 逐条（single）")


def test_failure_rows_are_rendered_not_dropped(rows):
    bad = ie._error_row(2, ie.PROBES[1], "single", "rate_limit: 429")
    lines: list[str] = []
    ie._case_table(lines, {"single": rows + [bad]})
    table = "\n".join(lines)
    assert "rate_limit" in table
    assert "本条件 2 条，其中调用失败 1 条" in table


def test_external_cases_load_and_score(tmp_path, harness):
    """`--cases` rows (real material, mapped gold) run through the same evaluate/score path; gold
    `none` is scored as a false-positive axis, never as a tag to hit."""
    import json

    provider, library = harness
    cases = {
        "schema": "intent-cases-v1",
        "label_map": {"version": "label-map-v1", "digest": "abc"},
        "source": "test",
        "cases": [
            {"id": "seg#1", "segment": "seg", "index": 1, "context": ["A: 周末有空吗"], "utterance": "改天吧", "gold_tags": ["委婉拒绝"], "gold_emotions": ["none"], "boundary": "none", "stratum": "gold"},
            {"id": "seg#2", "segment": "seg", "index": 2, "context": ["A: 到了吗"], "utterance": "到了", "gold_tags": ["none"], "gold_emotions": ["none"], "boundary": "none", "stratum": "gold"},
            {"id": "seg#3", "segment": "seg", "index": 3, "context": ["A: 在吗"], "utterance": "[图片]", "gold_tags": ["不确定"], "gold_emotions": ["不确定"], "boundary": "none", "stratum": "weak", "media_unknown": True},
        ],
    }
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    probes, note = ie.load_cases(f, "gold")
    assert len(probes) == 2 and "gold" in note and len(probes[0]) == 5
    rows = ie._run_condition(provider, probes, "context", library)
    assert {r["id"] for r in rows} == {"seg#1", "seg#2"}
    assert all(r["stratum"] == "gold" for r in rows)
    s = ie.score(rows)
    assert s["none_cases"] == 1 and s["none_false_positive_rate"] is not None
    assert s["strata"] == {"gold": 2}
    # a gold-none row never counts as a "full-label hit" denominator
    assert 0.0 <= s["superset_hit_rate_raw"] <= 1.0
    md = ie.render_markdown("fake", {"context": s}, {"context": rows}, cases=len(rows), slice_note="--cases cases.json")
    assert "极性错误率" in md and "none 误报率" in md and "--cases" in md
    # stratified sampling is seeded and reproducible
    p1, _ = ie.load_cases(f, "all", sample=2, seed=7)
    p2, _ = ie.load_cases(f, "all", sample=2, seed=7)
    assert [p[4]["id"] for p in p1] == [p[4]["id"] for p in p2]
