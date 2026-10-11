"""Read-only aggregate comparisons; compare exactly aligned tasks and examples."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def aggregate(root: Path, *, minimum_seeds=1):
    groups = defaultdict(list)
    for path in root.glob("*/report.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") != "completed" or len(report.get("experiments", [])) < 8:
            continue
        group = (report["dataset_id"], report["data"]["ids_hash"], report["config"]["visibility"], report["config"]["epochs"])
        groups[group].append(report)
    summaries = []
    for key, reports in groups.items():
        methods = defaultdict(dict)
        for report in reports:
            for experiment in report["experiments"]:
                methods[(experiment["method"], experiment["architecture"])][experiment["seed"]] = experiment
        rows = []
        for (method, arch), by_seed in sorted(methods.items()):
            if len(by_seed) < minimum_seeds:
                continue
            experiments = list(by_seed.values())
            scores = [e["test"]["macro_f1"] for e in experiments]
            rows.append({"method": method, "architecture": arch, "seeds": sorted(by_seed), "macro_f1_mean": statistics.mean(scores),
                         "macro_f1_std": statistics.stdev(scores) if len(scores) > 1 else None,
                         "emotion_macro_f1_mean": statistics.mean(e["test"]["tasks"]["emotion"]["macro_f1"] for e in experiments),
                         "intent_macro_f1_mean": statistics.mean(e["test"]["tasks"]["intent"]["macro_f1"] for e in experiments),
                         "context_head_latency_ms_p95_mean": statistics.mean(e["test"]["context_head_latency_ms_p95"] for e in experiments),
                         "trainable_parameters": experiments[0]["trainable_parameters"]})
        summaries.append({"dataset_id": key[0], "ids_hash": key[1], "visibility": key[2], "epochs": key[3],
                          "runs": [r["id"] for r in reports], "counts": reports[0]["data"]["counts"], "comparison": rows,
                          "production_enabled": False,
                          "limitations": ["标准差反映随机初始化波动，不是跨用户准确率置信区间", "只有两个测试来源组，不能证明人群泛化",
                                          "共享与分任务冻结同一中文编码器，仅上下文模块及任务头共享策略不同"]})
    return summaries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--minimum-seeds", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(aggregate(args.data_dir / "research" / "runs", minimum_seeds=args.minimum_seeds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
