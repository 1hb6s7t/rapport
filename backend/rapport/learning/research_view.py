"""Public experiment summaries only; never expose paths, private examples or weights."""
from __future__ import annotations

import json
import re
from pathlib import Path


def summaries(data_dir: Path):
    root = Path(data_dir) / "research" / "runs"
    if not root.is_dir():
        return []
    result = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        path = folder / "report.json"
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            output = {key: report[key] for key in ("id", "status", "dataset_id", "snapshot_sha256", "labels", "limitations", "peak_reserved_vram_bytes") if key in report}
            output["updated_at"] = path.stat().st_mtime
            output["data"] = {key: report.get("data", {}).get(key) for key in ("counts", "visibility", "split_protocol", "ids_hash", "labels", "production_eligible", "post_hoc_labels")}
            output["config"] = {key: report.get("config", {}).get(key) for key in ("epochs", "seed", "device", "max_context", "hidden", "batch_size")}
            output["experiments"] = [{key: experiment[key] for key in ("method", "architecture", "seed", "status", "error_type", "validation", "test", "thresholds", "history", "train_seconds", "trainable_parameters", "artifact_sha256", "paper_score_reproduction", "production_enabled") if key in experiment} for experiment in report.get("experiments", [])]
            baseline_path = folder / "llm-baseline.json"
            if baseline_path.is_file() and not baseline_path.is_symlink() and baseline_path.stat().st_size < 1024 * 1024:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                output["llm_baseline"] = {key: baseline[key] for key in ("method", "model", "input_visibility", "samples", "same_full_test_set", "test_ids_hash", "tasks", "macro_f1", "format_failure_rate", "latency_seconds_p95", "is_original_cloud_model", "production_enabled", "comparison_caveat", "aligned_neural_pilot", "none_policy", "abstention_rate", "training_condition", "includes_product_evidence_gates") if key in baseline}
            calibration_path = folder / "calibration.json"
            if calibration_path.is_file() and not calibration_path.is_symlink() and calibration_path.stat().st_size < 2 * 1024 * 1024:
                calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
                output["calibration"] = {"results": [{key: row[key] for key in ("run_id", "method", "architecture", "seed", "tasks", "limitations", "production_enabled", "validation_samples", "test_samples", "ids_hash") if key in row} for row in calibration.get("results", [])]}
            result.append(output)
        except (OSError, ValueError, TypeError):
            continue  # A running experiment may be between atomic report updates.
    return sorted(result, key=lambda item: item["updated_at"], reverse=True)[:30]


def interaction_summaries(data_dir: Path):
    root = Path(data_dir) / "research" / "interaction"
    if not root.is_dir():
        return []
    output = []
    for path in root.glob("*/summary.json"):
        if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction() or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("protocol") != "interaction-study-v2":
                continue
            report = {key: report[key] for key in ("id", "dataset_id", "status", "features", "methods", "versions", "sources", "limitations", "production_enabled", "protocol") if key in report}
            report["updated_at"] = path.stat().st_mtime
            output.append(report)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(output, key=lambda value: value["updated_at"], reverse=True)[:10]
