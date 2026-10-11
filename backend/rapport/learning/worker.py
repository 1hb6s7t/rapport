"""Allowlisted, resumable local jobs. Executed outside the web/analysis runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from rapport.learning import baseline
from rapport.learning.service import LearningService, digest


class JobCancelled(Exception):
    pass


def validate_split(rows: list[dict]):
    seen = {}
    groups = {}
    for row in rows:
        split = row.get("split")
        if split not in {"train", "validation", "test"}:
            continue
        ident = row["id"]
        if ident in seen and seen[ident] != split:
            raise ValueError("相同消息进入不同数据分区，已停止训练")
        seen[ident] = split
        # Analysis snapshots hold out entire friend/session groups.
        group = row.get("session_id")
        if group and group in groups and groups[group] != split:
            raise ValueError("相同会话进入不同数据分区，已停止训练")
        if group:
            groups[group] = split


def activation_gate(metrics: dict, rows: list[dict], pool: str) -> dict:
    reasons = []
    if metrics["samples"] < 30:
        reasons.append("test_samples_below_30")
    if metrics["macro_f1"] < .45:
        reasons.append("macro_f1_below_0.45")
    if metrics["macro_f1"] < metrics["majority_baseline"]["macro_f1"] + .01:
        reasons.append("no_improvement_over_majority_baseline")
    if metrics["accuracy_delta_ci95"][0] < 0:
        reasons.append("accuracy_improvement_uncertain")
    if pool == "realtime_review" and any(not r.get("human_confirmed") or r.get("post_hoc") for r in rows if baseline.eligible(r)):
        reasons.append("causal_human_review_required")
    # Baselines cannot claim to outperform the existing production analysis without its paired benchmark.
    reasons.append("production_baseline_comparison_pending")
    return {"passed": not reasons, "reasons": reasons, "scope": "personal_development",
            "majority_check_passed": not [r for r in reasons if r != "production_baseline_comparison_pending"]}


def _fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def safe_failure(error: Exception) -> str:
    """Keep worker failures actionable without persisting private sample text."""
    if isinstance(error, ValueError):
        return str(error)[:240]
    description = str(error).casefold()
    if "out of memory" in description or "cublas_status_alloc_failed" in description:
        return "gpu_out_of_memory: 显存不足，本轮未生成候选；请缩小训练预算或释放显存后重试"
    return f"{type(error).__name__}: 本地任务执行失败，请检查依赖和数据状态"


def _source_bindings(service, dataset_id, rows):
    from rapport.datasets.archives import learning_source_bindings

    store = service.datasets()
    # Synthetic worker adapters used by tests have no private archive bindings.
    return learning_source_bindings(store, dataset_id, [row["id"] for row in rows], verify_present=True) if hasattr(store, "_load") else []


def _register_checked(service, model, dataset_id, rows):
    model["source_ids"] = sorted(set(model.get("source_ids", [])) | set(_source_bindings(service, dataset_id, rows)))
    service.register(model)
    try:
        # Covers deletion between source validation and registry insertion.
        _source_bindings(service, dataset_id, rows)
        service.datasets().get(dataset_id)
    except Exception:
        service.invalidate_sources([f"dataset:{dataset_id}"])
        raise


def run(service: LearningService, job_id: str):
    if not service.claim(job_id):
        return
    job = service.job(job_id)

    def checkpoint(progress=0.0, stage="执行中"):
        current = service.job(job_id)
        if current["cancel_requested"]:
            raise JobCancelled()
        service.update_job(job_id, progress=round(max(0, min(1, progress)), 4), stage=stage)

    try:
        checkpoint()
        if job["kind"] == "dataset_build":
            manifest = service.datasets().build()
            checkpoint(.95, "快照构建完成")
            service.update_job(job_id, status="completed", progress=1, stage="数据快照已建立", result={"dataset_id": manifest["id"]})
            return
        if job["kind"] == "archive_prepare":
            from rapport.config import Settings
            from rapport.datasets.archives import append_authorized_archives
            from rapport.learning.incremental import job_scope, validate_scope
            from rapport.state import AppState

            scope = validate_scope(service, job_scope(service, job))
            checkpoint(.1, "读取已授权好友的长期归档")
            state = AppState(Settings(data_dir=service.data_dir))
            manifest = append_authorized_archives(state, relationship_ids=scope, base_dataset_id=job.get("dataset_id"))
            checkpoint(.95, "合格样本与固定留出分区已更新")
            service.update_job(job_id, status="completed", progress=1, stage="归档样本已准备，尚未训练", result={"dataset_id": manifest["id"], "delta": manifest.get("delta", {}), "unchanged": manifest.get("unchanged", False)})
            return
        manifest = service.datasets().get(job["dataset_id"])
        rows = service.datasets().records(job["dataset_id"], job["pool"])
        _source_bindings(service, job["dataset_id"], rows)
        checkpoint(.02, "核验分区与来源")
        if job["pool"] != "personal_reply":
            validate_split(rows)
        if job["architecture"] == "reply_lora":
            if job["kind"] == "evaluate":
                from rapport.learning.reply_runtime import evaluate_lora

                result = evaluate_lora(service, job, rows, checkpoint)
                service.update_job(job_id, status="completed", progress=1, stage="回复评测已完成，等待样例复核", result=result)
                return
            if job.get("parent_model_id"):
                from rapport.learning.preference import train_preference

                model = train_preference(service, job, rows, checkpoint)
            else:
                from rapport.learning.lora import train_lora

                model = train_lora(service, job, rows, checkpoint)
            _register_checked(service, model, job["dataset_id"], rows)
            checkpoint(.99, "适配器已保存，等待独立回复评测")
            service.update_job(job_id, status="completed", progress=1, stage="候选适配器已保存", result={"model_id": model["id"]})
            from rapport.learning.incremental import record_training_completion

            record_training_completion(service, job, model, rows)
            return
        data_hash = _fingerprint(rows)
        if job["kind"] == "train":
            model_id = job["id"]
            weights = None
            if job.get("retry_of"):
                previous = service.job(job["retry_of"])
                saved = previous.get("checkpoint")
                if saved and saved.get("dataset_hash") == data_hash:
                    model_id = saved["artifact_id"]
                    weights = service.load_artifact({"id": model_id, "artifact_hash": saved["artifact_hash"]})
            if weights is None:
                weights = baseline.train(rows, job["architecture"], job["max_features"],
                                         lambda p, s: checkpoint(.05 + p * .6, s))
            checkpoint(.66, "保存真实学习权重")
            artifact_hash = service.save_artifact(model_id, weights)
            service.update_job(job_id, checkpoint={"stage": "trained", "artifact_id": model_id,
                                                   "artifact_hash": artifact_hash, "dataset_hash": data_hash})
            validation = baseline.evaluate(weights, rows, "validation", lambda p, s: checkpoint(.68 + .29 * p, s))
            checkpoint(.98, "登记候选模型")
            source_ids = sorted({str(x) for r in rows for x in r.get("source_ids", [])} | {f"dataset:{job['dataset_id']}"})
            model = {"id": model_id, "task": "analysis", "architecture": job["architecture"], "method": "char_ngram_naive_bayes",
                     "state": "candidate", "pool": job["pool"], "scope": job["pool"], "dataset_id": job["dataset_id"],
                     "dataset_hash": data_hash, "manifest_hash": digest(manifest), "source_ids": source_ids,
                     "artifact_hash": artifact_hash, "base_model_hash": digest({"method": "char_nb", "alpha": 1}),
                     "tokenizer_hash": digest(weights["tokenizer"]), "metrics": {"validation": validation},
                     "gate": {"passed": False, "reasons": ["test_evaluation_required"]}, "runtime_supported": True,
                     "trained_samples": weights["training_samples"], "created_at": time.time(), "job_id": job_id,
                     "paper_replication": False, "label_provenance": "ai_assisted_not_human_gold"}
            # Re-check before registering: deletion during training must not leave an activatable model.
            service.datasets().get(job["dataset_id"])
            _register_checked(service, model, job["dataset_id"], rows)
            service.update_job(job_id, status="completed", progress=1, stage="候选模型已训练，尚未启用", result={"model_id": model_id})
            from rapport.learning.incremental import record_training_completion

            record_training_completion(service, job, model, rows)
        elif job["kind"] == "evaluate":
            model = service.model(job["model_id"])
            if model["dataset_hash"] != data_hash:
                raise ValueError("数据快照发生变化，不能复用原模型的评测资格")
            weights = service.load_artifact(model)
            metrics = baseline.evaluate(weights, rows, "test", lambda p, s: checkpoint(.05 + .9 * p, s))
            checkpoint(.98, "核验启用条件")
            gate = activation_gate(metrics, rows, model["pool"])
            service.evaluated(model["id"], metrics, gate)
            service.update_job(job_id, status="completed", progress=1, stage="评测已完成，不自动启用", result={"model_id": model["id"], "gate": gate})
    except JobCancelled:
        service.update_job(job_id, status="cancelled", stage="已取消，保留已完成阶段的检查点")
    except Exception as e:
        # Do not persist third-party exception messages which may include sample text or paths.
        service.update_job(job_id, status="failed", stage="任务失败", error=safe_failure(e))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    service = LearningService(args.data_dir, spawn_jobs=False)
    run(service, args.job)


if __name__ == "__main__":
    main()
