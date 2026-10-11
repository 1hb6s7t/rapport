"""Official MELD dyadic task sanity, with explicitly substituted sentence encoder."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from pathlib import Path

from rapport.research.data import digest
from rapport.research.lifecycle import ResearchRun
from rapport.research.public_data import read_meld_dyadic
from rapport.research.runner import code_provenance, feature_cache, train_one


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--encoder", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=2)
    args = parser.parse_args()
    import torch

    from rapport.security import Crypto

    torch.set_num_threads(2)
    examples, source = read_meld_dyadic(args.source, max_context=8, causal=False)
    examples.sort(key=lambda e: digest(e.id))
    selected = []
    counts = Counter()
    for e in examples:
        if counts[e.split] < {"train": 128, "validation": 32, "test": 64}[e.split]:
            selected.append(e)
            counts[e.split] += 1
    labels = {"emotion": ["anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise"]}
    config = {"seed": 20260922, "device": "cpu", "hidden": 64, "batch_size": 16, "visibility": "offline",
              "epochs": args.epochs, "classification": "single_label"}
    ident = uuid.uuid4().hex
    guard = ResearchRun(args.data_dir, ident, None)
    output = args.data_dir / "research" / "runs" / ident
    output.mkdir(parents=True)
    crypto = Crypto(args.data_dir / ".key")
    report = {"id": ident, "status": "running", "dataset_id": "public_MELD_Dyadic", "source": source,
              "provenance": code_provenance(), "config": config, "data": {"counts": dict(counts), "visibility": "offline", "ids_hash": digest([e.id for e in selected])},
              "experiments": [], "paper_score_reproduction": False,
              "limitations": ["公开原任务适配sanity；替换中文BGE编码器且缩小样本和训练轮数，不能与论文表格成绩比较", "未执行原作者预计算特征pickle", "不登记生产模型"]}
    path = output / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        vectors, info = feature_cache(selected, args.encoder, args.data_dir, crypto, "cpu", checkpoint=guard.check)
        report["encoder"] = info
        for method in ("dag_erc", "dialogue_gcn", "dialogue_crn"):
            report["experiments"].append(train_one(method, "shared", selected, vectors, labels, config, output, crypto, checkpoint=guard.check))
            guard.check()
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["status"] = "completed"
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        if output.exists():
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        guard.finish(report["status"])
    print(json.dumps({"run_id": ident, "status": report["status"]}), flush=True)


if __name__ == "__main__":
    main()
