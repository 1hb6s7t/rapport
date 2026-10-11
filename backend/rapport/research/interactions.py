"""Observed Chinese markers and offline change points; no affection score."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import re
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEATURES = ("other_message_share", "turn_switch_share", "mean_text_length", "log_reply_gap_seconds", "text_message_share")


def chinese_markers(text):
    import jieba

    from rapport.research.vendor.convokit_zh.strategy_extractor import (
        get_chinese_politeness_strategy_features,
    )

    sentences = [[{"tok": token} for token in jieba.cut(sentence, HMM=False) if token.strip()]
                 for sentence in re.split(r"(?<=[。！？!?])", text) if sentence.strip()]
    features, _ = get_chinese_politeness_strategy_features(sentences)
    return features


def timestamp(message):
    if not message.get("ts") or message.get("ts_precision") not in {"second", "minute"}:
        return None
    try:
        value = datetime.fromisoformat(message["ts"].replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))
    except (ValueError, TypeError):
        return None


def session_features(messages, gap_hours=6):
    selected = [m for m in messages if m.get("sender") in {"me", "other"} and timestamp(m)]
    blocks, block = [], []
    rejected = 0
    for message in selected:
        if block:
            gap = (timestamp(message) - timestamp(block[-1])).total_seconds()
            if gap < 0:
                rejected += 1
                continue
            if gap >= gap_hours * 3600:
                blocks.append(block)
                block = []
        block.append(message)
    if block:
        blocks.append(block)
    rows = []
    for i, group in enumerate(blocks):
        if len(group) < 4 or {m["sender"] for m in group} != {"me", "other"}:
            continue
        switches = [(a, b) for a, b in zip(group, group[1:], strict=False) if a["sender"] != b["sender"]]
        gaps = [(timestamp(b) - timestamp(a)).total_seconds() for a, b in switches]
        texts = [m.get("text") or "" for m in group if m.get("type") == "text"]
        rows.append({"session_index": i, "messages": len(group), "values": [
            sum(m["sender"] == "other" for m in group) / len(group),
            len(switches) / (len(group) - 1), statistics.mean(map(len, texts)) if texts else 0.,
            math.log1p(statistics.median(gaps)) if gaps else 0., len(texts) / len(group)],
            "evidence_ids": [m["id"] for m in group], "timing_note": "可观察的相邻轮次间隔；非真实心理反应速度"})
    return rows, {"input_messages": len(messages), "eligible_messages_with_times": len(selected), "nonmonotonic_rejected": rejected}


def detect_changes(values, penalties=(2., 4., 8., 12.)):
    import numpy as np
    import ruptures

    x = np.asarray(values, dtype=float)
    if x.ndim != 2 or len(x) < 8:
        return {"status": "insufficient_sessions", "runs": [], "stable_indices": []}
    if not np.isfinite(x).all():
        raise ValueError("Change-point input must be finite")
    scale = np.quantile(x, .75, axis=0) - np.quantile(x, .25, axis=0)
    normalized = (x - np.median(x, axis=0)) / np.where(scale > 1e-6, scale, 1.)
    runs = []
    for cost in ("l2", "rbf"):
        estimator = ruptures.Pelt(model=cost, min_size=3, jump=1).fit(normalized)
        for penalty in penalties:
            points = [i for i in estimator.predict(pen=penalty) if i < len(x)]
            runs.append({"cost": cost, "penalty": penalty, "indices": points})
    frequencies = Counter(i for run in runs for i in run["indices"])
    return {"status": "exploratory", "runs": runs,
            "stable_indices": [i for i, count in sorted(frequencies.items()) if count >= len(runs) // 2],
            "stability_note": "不同代价/惩罚下的参数敏感性，不是变化点正确概率",
            "descriptive_half_difference": (x[len(x) // 2:].mean(0) - x[:len(x) // 2].mean(0)).tolist()}


def run(data_dir: Path, dataset_id: str):
    from rapport.datasets import DatasetStore
    from rapport.research.lifecycle import ResearchRun
    from rapport.security import Crypto

    crypto = Crypto(data_dir / ".key")
    store = DatasetStore(data_dir / "datasets", crypto)
    manifest = store.get(dataset_id)
    audit_id = manifest.get("audit_id")
    if not audit_id:
        raise ValueError("此研究需要可读取完整会话的已审核源快照")
    corpus = store._load(audit_id)["corpus"]
    ident = uuid.uuid4().hex
    lifecycle = ResearchRun(data_dir, ident, dataset_id)
    output = data_dir / "research" / "interaction" / ident
    output.mkdir(parents=True, exist_ok=True)
    summaries, evidence = [], []
    start = time.perf_counter()
    try:
        for source in corpus["sessions"]:
            lifecycle.check()
            rows, coverage = session_features(source["messages"])
            totals = {speaker: Counter() for speaker in ("me", "other")}
            denominators = Counter()
            for i, message in enumerate(source["messages"]):
                if i % 200 == 0:
                    lifecycle.check()
                if message.get("sender") not in totals or message.get("type") != "text" or not message.get("text"):
                    continue
                speaker = message["sender"]
                features = chinese_markers(message["text"])
                totals[speaker].update(features)
                denominators[speaker] += 1
                active = [name for name, present in features.items() if present]
                if active:
                    evidence.append({"source_id": source["id"], "message_id": message["id"], "markers": active})
            changes = detect_changes([row["values"] for row in rows])
            summaries.append({"source_id": source["id"], "coverage": coverage, "sessions": len(rows),
                              "marker_counts": {key: dict(value) for key, value in totals.items()},
                              "marker_denominators": dict(denominators), "change_points": changes})
            evidence.append({"source_id": source["id"], "session_evidence": rows})
        lifecycle.check()
        (output / "evidence.bin").write_bytes(crypto.encrypt(json.dumps(evidence, ensure_ascii=False).encode()))
        summary = {"id": ident, "dataset_id": dataset_id, "status": "completed", "features": FEATURES, "protocol": "interaction-study-v2",
                   "config_hash": hashlib.sha256(json.dumps({"features": FEATURES, "gap_hours": 6, "penalties": [2, 4, 8, 12]}, sort_keys=True).encode()).hexdigest(),
                   "methods": ["ConvoKit Chinese CSCW markers (jieba tokenization)", "ruptures PELT l2/rbf"],
                   "versions": {name: importlib.metadata.version(name) for name in ("jieba", "ruptures")},
                   "elapsed_seconds": time.perf_counter() - start, "sources": summaries,
                   "limitations": ["礼貌词不等于喜欢程度", "无人工变化点标签，不能报告准确率或证明优于原趋势规则",
                                   "PELT使用完整时间序列，属于离线复盘，不是实时预测", "时间未显示的消息不补时间",
                                   "中文分词与原示例spaCy不同，不继承论文成绩"], "production_enabled": False}
        lifecycle.check()
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lifecycle.finish("completed")
        return summary
    except BaseException:
        lifecycle.finish("failed")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    args = parser.parse_args()
    result = run(args.data_dir, args.dataset_id)
    print(json.dumps({"id": result["id"], "sources": len(result["sources"]), "status": result["status"]}))


if __name__ == "__main__":
    main()
