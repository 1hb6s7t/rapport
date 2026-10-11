"""Disabled-by-default local schedules, immutable preparation and consumed IDs.

Private authorised relationship IDs and the trained-sample ledger are encrypted
inside the existing settings/jobs tables. No timer captures the desktop here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime

from rapport.errors import RapportError
from rapport.learning.timezones import schedule_zone

LOCK = threading.RLock()
DEFAULTS = {"enabled": False, "source_mode": "dataset", "relationship_ids": [], "timezone": "Asia/Shanghai", "dataset_id": None,
            "weekly_day": 0, "min_new_samples": 200, "pool": "offline_review", "architecture": "shared", "base_model_id": None,
            "last_inspected_at": None, "last_trained_at": None, "last_sample_count": 0, "last_new_samples": 0,
            "last_status": "disabled", "last_error": None, "prepare_job_id": None, "training_job_id": None,
            "last_training_attempt_at": None, "revision": "initial"}


def seal(service, value):
    return service.crypto.encrypt(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).decode()


def unseal(service, token, default=None):
    if not token:
        return default
    try:
        return json.loads(service.crypto.decrypt(token.encode()))
    except (ValueError, TypeError) as exc:
        raise RapportError("learning_private_state_invalid", "自动更新的授权或样本记录无法解密，请重新配置。", status=409) from exc


def validate_scope(service, relationship_ids):
    scope = sorted(set(relationship_ids or []))
    if not scope or len(scope) > 100 or any(not isinstance(rid, str) or not rid or len(rid) > 100 for rid in scope):
        raise RapportError("learning_archive_scope_required", "请明确选择允许整理长期归档的好友。", status=422)
    path = service.data_dir / "app.db"
    if not path.is_file():
        raise RapportError("learning_archive_scope_missing", "当前应用没有可授权的好友档案。", status=404)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if any(not connection.execute("SELECT 1 FROM relationships WHERE id=?", (rid,)).fetchone() for rid in scope):
            raise RapportError("learning_archive_scope_missing", "授权好友已不存在，请重新选择。", status=404)
    finally:
        connection.close()
    return scope


def job_scope(service, job):
    return unseal(service, job.get("archive_scope"), [])


def get_schedule(service):
    with service.db() as connection:
        item = service._get(connection, "settings", "schedule") or {}
    item = dict(item)
    item["relationship_ids"] = unseal(service, item.pop("archive_scope", None), item.get("relationship_ids", []))
    return {**DEFAULTS, **item}


def save_schedule(service, cfg):
    stored = {**cfg, "id": "schedule", "archive_scope": seal(service, cfg.get("relationship_ids", []))}
    stored.pop("relationship_ids", None)
    with service.db() as connection:
        service._put(connection, "settings", stored)


def set_schedule(service, values):
    import uuid

    allowed = {"enabled", "source_mode", "relationship_ids", "timezone", "dataset_id", "weekly_day", "min_new_samples", "pool", "architecture", "base_model_id"}
    if set(values) - allowed:
        raise RapportError("learning_schedule_invalid", "自动更新配置无效", status=400)
    with LOCK:
        old = get_schedule(service)
        cfg = {**old, **values}
        if not 0 <= cfg["weekly_day"] <= 6 or not 200 <= cfg["min_new_samples"] <= 100000:
            raise RapportError("learning_schedule_invalid", "自动更新配置无效", status=400)
        if cfg["source_mode"] not in {"dataset", "archives"} or cfg["architecture"] not in {"shared", "separate", "reply_lora"} or cfg["pool"] not in {"offline_review", "realtime_review", "personal_reply"}:
            raise RapportError("learning_schedule_invalid", "自动更新来源或任务无效", status=400)
        if (cfg["architecture"] == "reply_lora") != (cfg["pool"] == "personal_reply"):
            raise RapportError("learning_schedule_invalid", "训练架构与样本用途不一致", status=400)
        if cfg["enabled"]:
            schedule_zone(cfg["timezone"])
            if cfg["source_mode"] == "archives":
                cfg["relationship_ids"] = validate_scope(service, cfg["relationship_ids"])
                if cfg["pool"] != "personal_reply":
                    raise RapportError("learning_schedule_invalid", "长期归档更新首先用于本人表达模型。", status=422)
                if cfg["dataset_id"]:
                    service.datasets().get(cfg["dataset_id"])
            else:
                service.datasets().get(cfg["dataset_id"] or "")
            if cfg["architecture"] == "reply_lora" and (not cfg.get("base_model_id") or not service.capabilities()["lora_dependencies"]):
                raise RapportError("learning_schedule_invalid", "请先准备个人微调环境并选择基础模型", status=409)
        if any(cfg.get(key) != old.get(key) for key in allowed):
            cfg["revision"] = uuid.uuid4().hex
            cfg.update(last_inspected_at=None, last_status="idle" if cfg["enabled"] else "disabled", last_error=None)
            # Cancel preparation/training when permission or target changes. A
            # running task checks cancellation before publishing its result.
            for job in service.jobs():
                if job.get("scheduled") and job["status"] in {"queued", "running"}:
                    service.cancel(job["id"])
            cfg.update(prepare_job_id=None, training_job_id=None)
        save_schedule(service, cfg)
        return cfg


def sample_keys(rows):
    # Keys don't include snapshot ID. Changed snapshot metadata cannot turn the
    # same observed sample into new data. IDs also disambiguate real repetitions.
    from rapport.datasets.corpus import reply_split

    def split(row):
        return reply_split(row) if row.get("quality") == "observed_self_reply" else row.get("split")

    return {hashlib.sha256(json.dumps([row["id"], row.get("content_refs", []), row.get("response", row.get("text", ""))], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for row in rows if row.get("training_eligible") and split(row) == "train"}


def ledger_key(job):
    return ":".join([job["pool"], job["architecture"], job.get("base_model_id") or "none"])


def _ledger(service):
    with service.db() as connection:
        row = service._get(connection, "settings", "sample_ledger")
    return unseal(service, row.get("sealed") if row else None, {})


def _remember_completed(service, job):
    if job.get("samples_recorded"):
        return
    data = _ledger(service)
    keys = unseal(service, job.get("training_sample_keys"), [])
    if not keys and job.get("architecture") != "reply_lora":
        keys = sorted(sample_keys(service.datasets().records(job["dataset_id"], job["pool"])))
    key = ledger_key(job)
    data[key] = sorted(set(data.get(key, [])) | set(keys))
    with service.db() as connection:
        service._put(connection, "settings", {"id": "sample_ledger", "sealed": seal(service, data)})
    service.update_job(job["id"], samples_recorded=True)


def record_training_completion(service, job, model, rows):
    """Consume only actual training inputs, including manually started jobs."""
    if model.get("trained_sample_ids") is not None:
        selected = set(model["trained_sample_ids"])
        actual = [row for row in rows if row["id"] in selected]
    elif job["architecture"] != "reply_lora":
        actual = [row for row in rows if row.get("split") == "train" and row.get("training_eligible")]
    else:
        # A legacy adapter may report only a count. Do not fabricate which rows
        # its shuffled training budget consumed.
        service.update_job(job["id"], training_sample_keys=seal(service, []), training_sample_identity="unknown_legacy")
        return
    keys = sorted(sample_keys(actual))
    service.update_job(job["id"], training_sample_keys=seal(service, keys), training_sample_identity="verified", trained_sample_count=len(keys))
    _remember_completed(service, service.job(job["id"]))


def tick(service, now=None):
    now = time.time() if now is None else now
    with LOCK:
        cfg = get_schedule(service)
        if not cfg["enabled"]:
            return None
        try:
            jobs = service.jobs()
            for completed in jobs:
                if completed["kind"] == "train" and completed["status"] == "completed":
                    _remember_completed(service, completed)
                    if completed.get("scheduled"):
                        cfg["last_trained_at"] = max(cfg.get("last_trained_at") or 0, completed["updated_at"])
                    if completed.get("scheduled") and not completed.get("evaluation_job"):
                        evaluation = service.create_job("evaluate", model_id=completed["result"]["model_id"], scheduled=True, schedule_revision=cfg["revision"])
                        service.update_job(completed["id"], evaluation_job=evaluation["id"])
                        cfg["last_status"] = "evaluating"
                if not completed.get("scheduled"):
                    continue
                if completed["id"] == cfg.get("prepare_job_id") and completed["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    cfg["prepare_job_id"] = None
                    if completed["status"] == "completed" and completed.get("schedule_revision") == cfg["revision"]:
                        cfg["dataset_id"] = completed["result"]["dataset_id"]
                        cfg.update(last_error=None, last_status="ready", last_inspected_at=now)
                    else:
                        cfg.update(last_error=completed.get("error") or "归档整理已中断，可从任务列表重试。", last_status="failed", last_inspected_at=now)
                if completed["id"] == cfg.get("training_job_id") and completed["status"] in {"failed", "cancelled", "interrupted"}:
                    cfg.update(training_job_id=None, last_status="failed", last_error=completed.get("error") or "训练已中断，本轮数据尚未标记为已训练。")
            jobs = service.jobs()
            pending = any(job["status"] in {"queued", "running"} for job in jobs)
            zone = schedule_zone(cfg["timezone"])
            local_now = datetime.fromtimestamp(now, zone)
            last = cfg.get("last_inspected_at")
            check_due = last is None or datetime.fromtimestamp(last, zone).date() < local_now.date()
            if cfg["source_mode"] == "archives":
                validate_scope(service, cfg["relationship_ids"])
                if check_due and not pending:
                    prepared = service.create_job("archive_prepare", dataset_id=cfg["dataset_id"], source_mode="archives", relationship_ids=cfg["relationship_ids"],
                                                  pool="personal_reply", architecture="reply_lora", scheduled=True, schedule_revision=cfg["revision"])
                    cfg.update(prepare_job_id=prepared["id"], last_inspected_at=now, last_status="preparing", last_error=None)
                    save_schedule(service, cfg)
                    return prepared
            elif check_due:
                datasets = service.datasets()
                if hasattr(datasets, "refresh"):
                    cfg["dataset_id"] = datasets.refresh(cfg["dataset_id"])["id"]
                cfg.update(last_inspected_at=now, last_status="ready", last_error=None)
            if not cfg.get("dataset_id"):
                save_schedule(service, cfg)
                return None
            rows = service.datasets().records(cfg["dataset_id"], cfg["pool"])
            keys = sample_keys(rows)
            trained = set(_ledger(service).get(ledger_key(cfg), []))
            fresh = keys - trained
            cfg.update(last_new_samples=len(fresh), last_sample_count=len(keys))
            last_trained = cfg.get("last_trained_at")
            last_attempt = cfg.get("last_training_attempt_at")
            due = local_now.weekday() == cfg["weekly_day"] and (not last_trained or (local_now.date() - datetime.fromtimestamp(last_trained, zone).date()).days >= 6) and (not last_attempt or now - last_attempt >= 86400)
            if due and not pending and len(fresh) >= cfg["min_new_samples"]:
                job = service.create_job("train", dataset_id=cfg["dataset_id"], pool=cfg["pool"], architecture=cfg["architecture"], base_model_id=cfg.get("base_model_id"),
                                         source_mode=cfg["source_mode"], relationship_ids=cfg["relationship_ids"], scheduled=True, schedule_revision=cfg["revision"])
                cfg.update(training_job_id=job["id"], last_status="training", last_error=None, last_training_attempt_at=now)
                save_schedule(service, cfg)
                return job
            save_schedule(service, cfg)
            return None
        except (OSError, ValueError, RapportError) as exc:
            cfg.update(last_status="failed", last_error=exc.code if isinstance(exc, RapportError) else "local_update_failed", last_inspected_at=now)
            if isinstance(exc, RapportError) and exc.code in {"learning_archive_scope_missing", "dataset_archive_source_revoked", "dataset_not_found"}:
                cfg["enabled"] = False
            save_schedule(service, cfg)
            return None
